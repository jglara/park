#[macro_use]
extern crate log;
use log4rs;

pub mod data_capnp {
    include!(concat!(env!("OUT_DIR"), "/data_capnp.rs"));
}

use clap::Parser;
//use quiche::PathStats;
use ring::rand::*;

use std::collections::HashMap;
use std::net::{self, SocketAddr};

use crate::data_capnp::{data, scheduler};
use capnp_rpc::{
    rpc_twoparty_capnp::{self, Side},
    twoparty, RpcSystem,
};
use futures::AsyncReadExt;
use std::net::ToSocketAddrs;

use quiche::PathStats;
use tokio::sync::mpsc;

#[derive(Parser)]
#[clap(author, version, about, long_about = None)]
struct ServerCli {
    #[clap(value_parser, long, short)]
    listen: String, //Listen on the given IP:port [default: 127.0.0.1:4433]

    #[clap(value_parser, long, short)]
    cert: String, // TLS certificate path [default: src/bin/cert.crt]

    #[clap(value_parser, long, short)]
    key: String, //TLS certificate key path [default: src/bin/cert.key]

    #[clap(value_parser, long, short)]
    root: String, // path of root server

    #[clap(value_parser, long, short)]
    scheduler: String, // Type of scheduler

    #[clap(value_parser, long)]
    logging_config: String, // log4rs logging config
}

const MAX_BUF_SIZE: usize = 65507;
const MAX_DATAGRAM_SIZE: usize = 1350;
struct PartialResponse {
    body: Vec<u8>,

    written: usize,
}
struct Client {
    client_id: u64,
    conn: quiche::Connection,
    partial_responses: HashMap<u64, PartialResponse>,
    loss_rate: f64,
    max_send_burst: usize,
}

type ClientId = u64;
type ClientIdMap = HashMap<quiche::ConnectionId<'static>, ClientId>;
type ClientMap = HashMap<ClientId, Client>;

trait Scheduler {
    fn start(&mut self, conn: &quiche::Connection);

    /// Return the next path
    fn next_path(
        &mut self,
        conn: &quiche::Connection,
    ) -> Option<(std::net::SocketAddr, std::net::SocketAddr)>;

    fn reset(&mut self, conn: &quiche::Connection);
}

struct RoundRobinScheduler {
    next: usize,
}

impl Scheduler for RoundRobinScheduler {
    fn start(&mut self, conn: &quiche::Connection) {
        self.next = conn.path_stats().count();
    }

    fn next_path(
        &mut self,
        conn: &quiche::Connection,
    ) -> Option<(std::net::SocketAddr, std::net::SocketAddr)> {
        self.next = if self.next >= conn.path_stats().count() {
            0
        } else {
            self.next + 1
        };
        //conn.path_stats().cycle().filter(|p| p.active && p.bytes_in_flight < p.cwnd).nth(self.next).map(|p| (p.local_addr, p.peer_addr))
        conn.path_stats()
            .nth(self.next)
            .map(|p| (p.local_addr, p.peer_addr))
    }

    fn reset(&mut self, conn: &quiche::Connection){}
}

struct MinRttScheduler {}

impl Scheduler for MinRttScheduler {
    fn start(&mut self, _conn: &quiche::Connection) {}

    fn next_path(
        &mut self,
        conn: &quiche::Connection,
    ) -> Option<(std::net::SocketAddr, std::net::SocketAddr)> {
        // always try each path at least once
        if let Some(p) = conn.path_stats().find(|p| p.sent < 3) {
            Some((p.local_addr, p.peer_addr))
        } else {
            conn.path_stats()
                .filter(|p| p.active && p.bytes_in_flight < p.cwnd)
                .min_by(|p1, p2| p1.rtt.cmp(&p2.rtt))
                .map(|p| (p.local_addr, p.peer_addr))
        }
    }
    fn reset (&mut self, conn: &quiche::Connection){}
}

struct RLScheduler {
    tx: mpsc::Sender<Data>,
    rx: mpsc::Receiver<u8>,
    prev_best_acked: usize,
    prev_second_acked: usize,
    path: Option<(std::net::SocketAddr, std::net::SocketAddr)>,
    
}

impl RLScheduler {
    fn calc_last_acked(&self, path_stats: &PathStats, last_total_acked: usize) -> usize 
    {
        let total_acked= if path_stats.sent_bytes as usize > path_stats.bytes_in_flight  {path_stats.sent_bytes as usize - path_stats.bytes_in_flight } else {0};
        if total_acked > last_total_acked {total_acked - last_total_acked} else {0}
    }
}
impl Scheduler for RLScheduler {


    fn start(&mut self, conn: &quiche::Connection) {
        let best_path = conn
            .path_stats()
            .filter(|p| p.active)
            .min_by(|p1, p2| p1.rtt.cmp(&p2.rtt))
            .unwrap();
        let second_path = conn
            .path_stats()
            .filter(|p| p.active)
            .max_by(|p1, p2| p1.rtt.cmp(&p2.rtt))
            .unwrap();

        /*if conn.path_stats().count() > 0 {
            info!("paths: {:?} first: {:?}", conn.path_stats().count(), conn.path_stats().nth(0).unwrap().peer_addr);
        }

        if conn.path_stats().count() > 1 {
            info!("paths: {:?} second: {:?}", conn.path_stats().count(), conn.path_stats().nth(1).unwrap().peer_addr);
        }
        */


        let data = Data {
            best_rtt: best_path.rtt.as_millis() as usize,
            second_rtt: second_path.rtt.as_millis() as usize,
            best_acked: self.calc_last_acked(&best_path, self.prev_best_acked) ,
            second_acked: self.calc_last_acked(&second_path, self.prev_second_acked) ,
            done: false,
        };
        self.prev_best_acked = best_path.sent_bytes as usize - best_path.bytes_in_flight as usize;
        self.prev_second_acked =
            second_path.sent_bytes as usize - second_path.bytes_in_flight as usize;
        self.tx.blocking_send(data).ok().unwrap();

        self.path = if let Some(resp) = self.rx.blocking_recv() {
            if resp == 0 {
                Some((best_path.local_addr, best_path.peer_addr))
            } else if resp == 1 {
                Some((second_path.local_addr, second_path.peer_addr))
            } else {
                None
            }
        } else {
            None
        }

    }
    
    fn next_path(
        &mut self,
        _conn: &quiche::Connection,
    ) -> Option<(std::net::SocketAddr, std::net::SocketAddr)> {
        self.path.clone()
    }

    fn reset(&mut self, conn: &quiche::Connection) {
        let best_path = conn
            .path_stats()
            .filter(|p| p.active)
            .min_by(|p1, p2| p1.rtt.cmp(&p2.rtt))
            .unwrap();

        let second_path = conn
            .path_stats()
            .filter(|p| p.active)
            .max_by(|p1, p2| p1.rtt.cmp(&p2.rtt))
            .unwrap();

        let data = Data {
            best_rtt: best_path.rtt.as_millis() as usize,
            second_rtt: second_path.rtt.as_millis() as usize,
            best_acked: self.calc_last_acked(&best_path, self.prev_best_acked) ,
            second_acked: self.calc_last_acked(&second_path, self.prev_second_acked) ,
            done: true,
        };

        self.prev_best_acked = best_path.sent_bytes as usize - best_path.bytes_in_flight as usize;
        self.prev_second_acked =
            second_path.sent_bytes as usize - second_path.bytes_in_flight as usize;
        self.tx.blocking_send(data).ok().unwrap();
        self.rx.blocking_recv(); // ignore the response

        self.prev_best_acked = 0;
        self.prev_second_acked = 0;

    }

}



struct Data {
    best_rtt: usize,
    second_rtt: usize,
    best_acked: usize,
    second_acked: usize,
    done: bool,
}
fn main() -> Result<(), Box<dyn std::error::Error>> {    
    let cli = ServerCli::parse();

    log4rs::init_file(cli.logging_config, Default::default()).unwrap();

    info!(
        "starting up server in {:?} with cert {} and key {}",
        cli.listen, cli.cert, cli.key
    );

    // Setup the event loop.
    let mut poll = mio::Poll::new().unwrap();
    let mut events = mio::Events::with_capacity(1024);
    let mut buf = [0; 65535];
    let mut out = [0; MAX_DATAGRAM_SIZE];

    let server_start = std::time::Instant::now();

    // Create the UDP listening socket, and register it with the event loop.
    let mut socket = mio::net::UdpSocket::bind(cli.listen.parse().unwrap()).unwrap();

    poll.registry()
        .register(&mut socket, mio::Token(0), mio::Interest::READABLE)
        .unwrap();

    // Create the configuration for the QUIC connections.
    let mut config = quiche::Config::new(quiche::PROTOCOL_VERSION).unwrap();

    config.load_cert_chain_from_pem_file(&cli.cert).unwrap();
    config.load_priv_key_from_pem_file(&cli.key).unwrap();

    config
        .set_application_protos(&[b"hq-interop", b"hq-29", b"hq-28", b"hq-27", b"http/0.9"])
        .unwrap();

    config.set_max_idle_timeout(5000);
    config.set_max_recv_udp_payload_size(MAX_DATAGRAM_SIZE);
    config.set_max_send_udp_payload_size(MAX_DATAGRAM_SIZE);
    config.set_initial_max_data(10_000_000);
    config.set_initial_max_stream_data_bidi_local(1_500_000);
    config.set_initial_max_stream_data_bidi_remote(1_500_000);
    config.set_initial_max_stream_data_uni(1_000_000);
    config.set_initial_max_streams_bidi(100);
    config.set_initial_max_streams_uni(100);
    config.set_disable_active_migration(true);
    config.enable_early_data();
    config.set_multipath(true);

    let rng = SystemRandom::new();
    let conn_id_seed = ring::hmac::Key::generate(ring::hmac::HMAC_SHA256, &rng).unwrap();

    let local_addr = socket.local_addr().unwrap();

    let mut next_client_id = 0;
    let mut clients_ids = ClientIdMap::new();
    let mut clients = ClientMap::new();

    let mut continue_write = false;
    let mut tx_cap = 0;

    let (tx_c, mut rx_m) = mpsc::channel::<u8>(64);
    let (tx_m, mut rx_c) = mpsc::channel::<Data>(64);

    let mut sched: Box<dyn Scheduler> = match cli.scheduler.as_str() {
        "rr" => Box::new(RoundRobinScheduler { next: 0 }),
        "minRtt" => Box::new(MinRttScheduler {}),
        "rl" => Box::new(RLScheduler {
            rx: rx_m,
            tx: tx_m,
            prev_best_acked: 0,
            prev_second_acked: 0,
            path: None,
        }),
        _ => panic!("Invalid scheduler"),
    };

    //Creation of thread for scheduler RPC

    let addr = "0.0.0.0:6677"
        .to_socket_addrs()
        .unwrap()
        .next()
        .expect("could not parse address");
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;

    if cli.scheduler.as_str() == "rl" {
        std::thread::spawn(move || {
            rt.block_on(async move {
                let stream = tokio::net::TcpStream::connect(&addr).await.unwrap();

                //println!("Connected to TCP Stream");

                stream.set_nodelay(true).unwrap();
                let (r, w) = tokio_util::compat::TokioAsyncReadCompatExt::compat(stream).split();

                let network = twoparty::VatNetwork::new(
                    r,
                    w,
                    rpc_twoparty_capnp::Side::Client,
                    Default::default(),
                );

                let mut rpc_system = RpcSystem::new(Box::new(network), None);
                let scheduler: scheduler::Client =
                    rpc_system.bootstrap(rpc_twoparty_capnp::Side::Server);

                tokio::task::LocalSet::new()
                    .run_until(async {
                        tokio::task::spawn_local(rpc_system);

                        while let Some(recv_data) = rx_c.recv().await {
                            let mut request = scheduler.next_path_request();

                            let mut msg = ::capnp::message::Builder::new_default();
                            let mut d = msg.init_root::<data::Builder>();
                            d.set_best_rtt(recv_data.best_rtt.try_into().unwrap());
                            d.set_second_rtt(recv_data.second_rtt.try_into().unwrap());
                            d.set_best_acked(recv_data.best_acked.try_into().unwrap());
                            d.set_second_acked(recv_data.second_acked.try_into().unwrap());
                            d.set_done(recv_data.done);

                            request.get().set_d(d.into_reader()).unwrap();

                            let reply = request.send().promise.await.unwrap();

                            tx_c.send(reply.get().unwrap().get_path()).await.unwrap();
                        }
                    })
                    .await
            });
        });
    }

    'main: loop {
        let timeout = match continue_write {
            true => Some(std::time::Duration::from_secs(0)),

            false => clients.values().filter_map(|c| c.conn.timeout()).min(),
        };

        let _res = poll.poll(&mut events, timeout);

        // Read incoming UDP packets from the socket and feed them to quiche,
        // until there are no more packets to read.
        'read: loop {
            // If the event loop reported no events, it means that the timeout
            // has expired, so handle it without attempting to read packets. We
            // will then proceed with the send loop.
            if events.is_empty() && !continue_write {
                debug!("timed out");
                clients.values_mut().for_each(|c| c.conn.on_timeout());

                break 'read;
            }

            let (len, from) = match socket.recv_from(&mut buf) {
                Ok(v) => v,

                Err(e) => {
                    // There are no more UDP packets to read, so end the read
                    // loop.
                    if e.kind() == std::io::ErrorKind::WouldBlock {
                        debug!("recv() would block");
                        break 'read;
                    }

                    panic!("recv() failed: {:?}", e);
                }
            };

            debug!("got {} bytes", len);

            let pkt_buf = &mut buf[..len];

            // Parse the QUIC packet's header.
            let hdr = match quiche::Header::from_slice(pkt_buf, quiche::MAX_CONN_ID_LEN) {
                Ok(v) => v,

                Err(e) => {
                    error!("Parsing packet header failed: {:?}", e);
                    continue 'read;
                }
            };

            trace!("got packet {:?}", hdr);

            let conn_id = ring::hmac::sign(&conn_id_seed, &hdr.dcid);
            let conn_id = &conn_id.as_ref()[..quiche::MAX_CONN_ID_LEN];
            let conn_id: quiche::ConnectionId = conn_id.to_vec().into();

            // Lookup a connection based on the packet's connection ID. If there
            // is no connection matching, create a new one.
            let client = if !clients_ids.contains_key(&hdr.dcid)
                && !clients_ids.contains_key(&conn_id)
            {
                if hdr.ty != quiche::Type::Initial {
                    error!("Packet dcid:{:?} {:?} is not Initial", hdr.dcid, conn_id);
                    continue 'read;
                }

                if !quiche::version_is_supported(hdr.version) {
                    warn!("Doing version negotiation");

                    let len = quiche::negotiate_version(&hdr.scid, &hdr.dcid, &mut out).unwrap();

                    let out = &out[..len];

                    if let Err(e) = socket.send_to(out, from) {
                        if e.kind() == std::io::ErrorKind::WouldBlock {
                            debug!("send() would block");
                            break;
                        }

                        panic!("send() failed: {:?}", e);
                    }
                    continue 'read;
                }

                let mut scid = [0; quiche::MAX_CONN_ID_LEN];
                scid.copy_from_slice(&conn_id);

                let scid = quiche::ConnectionId::from_ref(&scid);

                // Token is always present in Initial packets.
                let token = hdr.token.as_ref().unwrap();

                // Do stateless retry if the client didn't send a token.
                if token.is_empty() {
                    warn!("Doing stateless retry");

                    let new_token = mint_token(&hdr, &from);

                    let len = quiche::retry(
                        &hdr.scid,
                        &hdr.dcid,
                        &scid,
                        &new_token,
                        hdr.version,
                        &mut out,
                    )
                    .unwrap();

                    let out = &out[..len];

                    if let Err(e) = socket.send_to(out, from) {
                        if e.kind() == std::io::ErrorKind::WouldBlock {
                            debug!("send() would block");
                            break;
                        }

                        panic!("send() failed: {:?}", e);
                    }
                    continue 'read;
                }

                let odcid = validate_token(&from, token);

                // The token was not valid, meaning the retry failed, so
                // drop the packet.
                if odcid.is_none() {
                    error!("Invalid address validation token");
                    continue 'read;
                }

                if scid.len() != hdr.dcid.len() {
                    error!("Invalid destination connection ID");
                    continue 'read;
                }

                // Reuse the source connection ID we sent in the Retry packet,
                // instead of changing it again.
                let scid = hdr.dcid.clone();

                debug!("New connection: dcid={:?} scid={:?}", hdr.dcid, scid);

                let mut conn =
                    quiche::accept(&scid, odcid.as_ref(), local_addr, from, &mut config).unwrap();

                if let Some(dir) = std::env::var_os("QLOGDIR") {
                    let id = format!("{:?}", &scid);
                    let writer = make_qlog_writer(&dir, "server", &id);

                    conn.set_qlog(
                        std::boxed::Box::new(writer),
                        "quiche-server qlog".to_string(),
                        format!("{} id={}", "quiche-server qlog", id),
                    );
                }

                let client_id = next_client_id;

                clients.insert(
                    client_id,
                    Client {
                        client_id,
                        conn,
                        partial_responses: HashMap::new(),
                        loss_rate: 0.0,
                        max_send_burst: MAX_BUF_SIZE * 75 / 100,
                    },
                );

                clients_ids.insert(scid.clone(), client_id);
                next_client_id += 1;

                info!("Adding client: id: {:?} -> cid: {:?} ", client_id, scid);

                clients.get_mut(&client_id).unwrap()
            } else {
                debug!("Incoming packet for connection dcid={:?} ", hdr.dcid);
                let cid = match clients_ids.get(&hdr.dcid) {
                    Some(v) => v,

                    None => clients_ids.get(&conn_id).unwrap(),
                };

                clients.get_mut(cid).unwrap()
            };

            let recv_info = quiche::RecvInfo {
                to: socket.local_addr().unwrap(),
                from,
            };

            // Process potentially coalesced packets.
            let read = match client.conn.recv(pkt_buf, recv_info) {
                Ok(v) => v,

                Err(e) => {
                    error!("{} recv failed: {:?}", client.conn.trace_id(), e);
                    continue 'read;
                }
            };

            debug!("{} processed {} bytes", client.conn.trace_id(), read);
            if client.conn.is_in_early_data() || client.conn.is_established() {
                // Process all readable streams.
                for s in client.conn.readable() {
                    while let Ok((read, fin)) = client.conn.stream_recv(s, &mut buf) {
                        debug!("{} received {} bytes", client.conn.trace_id(), read);

                        let stream_buf = &buf[..read];

                        debug!(
                            "{} stream {} has {} bytes (fin? {})",
                            client.conn.trace_id(),
                            s,
                            stream_buf.len(),
                            fin
                        );

                        handle_stream(client, s, stream_buf, &cli.root);
                    }
                }
            }

            handle_path_events(client);

            // See whether source Connection IDs have been retired.
            while let Some(retired_scid) = client.conn.retired_scid_next() {
                info!("Retiring source CID {:?}", retired_scid);
            }

            // Provides as many CIDs as possible.
            while client.conn.source_cids_left() > 0 {
                let (scid, reset_token) = generate_cid_and_reset_token(&rng);
                if client
                    .conn
                    .new_source_cid(&scid, reset_token, false)
                    .is_err()
                {
                    break;
                }

                info!("Adding new source CID {:?} to {:?}", scid, client.client_id);
                clients_ids.insert(scid.clone(), client.client_id);


            }
        } //read loop

        //debug!("{} done reading", client.conn.trace_id());

        continue_write = false;
        // Generate outgoing QUIC packets for all active connections and send
        // them on the UDP socket, until quiche reports that there are no more
        // packets to be sent.
        for client in clients.values_mut() {
            tx_cap = client.conn.tx_cap;
            let mut max_off = 0;
            let mut off_back = 0;
            // Handle writable streams.

            for stream_id in client.conn.writable() {
                //info!("cap {:?} {:?} {:?}", client.conn.tx_cap, client.conn.tx_data, client.conn.max_tx_data);
                handle_writable(client, stream_id);
                if let Ok((max_off_, off_back_)) = client.conn.stream_send_offset(stream_id) {
                    max_off = max_off_;
                    off_back = off_back_;
                }
            }

            let max_datagram_size = client.conn.max_send_udp_payload_size();
            // Reduce max_send_burst by 25% if loss is increasing more than 0.1%.
            let loss_rate = client.conn.stats().lost as f64 / client.conn.stats().sent as f64;
            if loss_rate > client.loss_rate + 0.001 {
                client.max_send_burst = client.max_send_burst / 4 * 3;
                // Minimun bound of 10xMSS.
                client.max_send_burst = client.max_send_burst.max(max_datagram_size * 10);
                client.loss_rate = loss_rate;
            }

            let max_send_burst = /*client.max_send_burst / max_datagram_size * max_datagram_size;*/
                 client.conn.send_quantum().min(client.max_send_burst) /
                    max_datagram_size *
                    max_datagram_size;
            let mut total_write = 0;

            sched.start(&client.conn);

            'write: while total_write < max_send_burst {
                if let Some((local_addr, peer_addr)) = sched.next_path(&client.conn) {
                    let (write, send_info) =
                        match client
                            .conn
                            .send_on_path(&mut out, Some(local_addr), Some(peer_addr))
                        {
                            Ok(v) => v,

                            Err(quiche::Error::Done) => {
                                debug!("{} done writing", client.conn.trace_id());
                                break 'write;
                            }

                            Err(e) => {
                                error!("{} send failed: {:?}", client.conn.trace_id(), e);

                                client.conn.close(false, 0x1, b"fail").ok();
                                break 'write;
                            }
                        };

                    total_write += write;

                    if let Err(e) = socket.send_to(&out[..write], send_info.to) {
                        if e.kind() == std::io::ErrorKind::WouldBlock {
                            debug!("send() would block");
                            continue 'write;
                        }

                        panic!("send() failed: {:?}", e);
                    }

                    debug!(
                        "{}: {} -> {} written {} bytes",
                        client.conn.trace_id(),
                        local_addr,
                        send_info.to,
                        write
                    );
                } else {
                    debug!("No path to send");
                    break 'write;
                }

                if total_write >= max_send_burst {
                    debug!("{} pause writing", client.conn.trace_id(),);
                    continue_write = true;
                }
            }

            client.conn.path_stats().for_each(|p| {
                let (srtt, rttvar) = if p.rtt.as_millis() == 333 {
                    (0, 0)
                } else {
                    (p.rtt.as_millis(), p.rttvar.as_millis())
                };
            });

            //info!("cap {:?} {:?} {:?}", client.conn.tx_cap, client.conn.tx_data, client.conn.max_tx_data);
        }

        // Garbage collect closed connections.
        clients.retain(|_, ref mut c| {
            trace!("Collecting garbage");

            if c.conn.is_closed() {
           /*     info!(
                    "{} connection collected {:?} {:?}",
                    c.conn.trace_id(),
                    c.conn.stats(),
                    c.conn.path_stats().collect::<Vec<quiche::PathStats>>()
                );*/
               sched.reset(&c.conn);
               
            }

            !c.conn.is_closed()
        });

        clients_ids.retain(|_, client_id| clients.contains_key(client_id));
    }
}

/// Generate a stateless retry token.
///
/// The token includes the static string `"quiche"` followed by the IP address
/// of the client and by the original destination connection ID generated by the
/// client.
///
/// Note that this function is only an example and doesn't do any cryptographic
/// authenticate of the token. *It should not be used in production system*.
fn mint_token(hdr: &quiche::Header, src: &net::SocketAddr) -> Vec<u8> {
    let mut token = Vec::new();

    token.extend_from_slice(b"quiche");

    let addr = match src.ip() {
        std::net::IpAddr::V4(a) => a.octets().to_vec(),
        std::net::IpAddr::V6(a) => a.octets().to_vec(),
    };

    token.extend_from_slice(&addr);
    token.extend_from_slice(&hdr.dcid);

    token
}

/// Validates a stateless retry token.
///
/// This checks that the ticket includes the `"quiche"` static string, and that
/// the client IP address matches the address stored in the ticket.
///
/// Note that this function is only an example and doesn't do any cryptographic
/// authenticate of the token. *It should not be used in production system*.
fn validate_token<'a>(src: &net::SocketAddr, token: &'a [u8]) -> Option<quiche::ConnectionId<'a>> {
    if token.len() < 6 {
        return None;
    }

    if &token[..6] != b"quiche" {
        return None;
    }

    let token = &token[6..];

    let addr = match src.ip() {
        std::net::IpAddr::V4(a) => a.octets().to_vec(),
        std::net::IpAddr::V6(a) => a.octets().to_vec(),
    };

    if token.len() < addr.len() || &token[..addr.len()] != addr.as_slice() {
        return None;
    }

    Some(quiche::ConnectionId::from_ref(&token[addr.len()..]))
}

/// Handles incoming HTTP/0.9 requests.
fn handle_stream(client: &mut Client, stream_id: u64, buf: &[u8], root: &str) -> usize {
    let conn = &mut client.conn;

    if buf.len() > 4 && &buf[..4] == b"GET " {
        let uri = &buf[4..buf.len()];
        let uri = String::from_utf8(uri.to_vec()).unwrap();
        let uri = String::from(uri.lines().next().unwrap());
        let uri = std::path::Path::new(&uri);
        let mut path = std::path::PathBuf::from(root);

        for c in uri.components() {
            if let std::path::Component::Normal(v) = c {
                path.push(v)
            }
        }

        info!(
            "{} got GET request for {:?} on stream {}",
            conn.trace_id(),
            path,
            stream_id
        );

        let body = std::fs::read(path.as_path()).unwrap_or_else(|_| b"Not Found!\r\n".to_vec());

        info!(
            "{} sending response of size {} on stream {}",
            conn.trace_id(),
            body.len(),
            stream_id
        );

        let written = match conn.stream_send(stream_id, &body, true) {
            Ok(v) => v,

            Err(quiche::Error::Done) => 0,

            Err(e) => {
                error!("{} stream send failed {:?}", conn.trace_id(), e);
                return 0;
            }
        };

        if written < body.len() {
            let response = PartialResponse { body, written };
            client.partial_responses.insert(stream_id, response);
        }
        return written;
    }

    return 0;
}

/// Handles newly writable streams.
fn handle_writable(client: &mut Client, stream_id: u64) -> usize {
    let conn = &mut client.conn;

    debug!("{} stream {} is writable", conn.trace_id(), stream_id);

    if !client.partial_responses.contains_key(&stream_id) {
        debug!("{} stream with no partial responses", stream_id);
        return 0;
    }

    let resp = client.partial_responses.get_mut(&stream_id).unwrap();
    let body = &resp.body[resp.written..];

    let written = match conn.stream_send(stream_id, body, true) {
        Ok(v) => v,

        Err(quiche::Error::Done) => 0,

        Err(e) => {
            client.partial_responses.remove(&stream_id);

            error!("{} stream send failed {:?}", conn.trace_id(), e);
            return 0;
        }
    };

    resp.written += written;
    debug!(
        "Wrote {} bytes. pending {}",
        written,
        resp.body.len() - resp.written
    );

    if resp.written == resp.body.len() {
        client.partial_responses.remove(&stream_id);
    }

    written
}

fn handle_path_events(client: &mut Client) {
    while let Some(qe) = client.conn.path_event_next() {
        info!("Path event {:?}", qe);
        match qe {
            quiche::PathEvent::New(local_addr, peer_addr) => {
                info!(
                    "{} Seen new path ({}, {})",
                    client.conn.trace_id(),
                    local_addr,
                    peer_addr
                );

                // Directly probe the new path.
                client
                    .conn
                    .probe_path(local_addr, peer_addr)
                    .expect("cannot probe");
            }

            quiche::PathEvent::Validated(local_addr, peer_addr) => {
                info!(
                    "{} Path ({}, {}) is now validated",
                    client.conn.trace_id(),
                    local_addr,
                    peer_addr
                );
                client.conn.set_active(local_addr, peer_addr, true).ok();
            }

            quiche::PathEvent::FailedValidation(local_addr, peer_addr) => {
                info!(
                    "{} Path ({}, {}) failed validation",
                    client.conn.trace_id(),
                    local_addr,
                    peer_addr
                );
            }

            quiche::PathEvent::Closed(local_addr, peer_addr, err, reason) => {
                info!(
                    "{} Path ({}, {}) is now closed and unusable; err = {} reason = {:?}",
                    client.conn.trace_id(),
                    local_addr,
                    peer_addr,
                    err,
                    reason,
                );
            }

            quiche::PathEvent::ReusedSourceConnectionId(cid_seq, old, new) => {
                info!(
                    "{} Peer reused cid seq {} (initially {:?}) on {:?}",
                    client.conn.trace_id(),
                    cid_seq,
                    old,
                    new
                );
            }

            quiche::PathEvent::PeerMigrated(local_addr, peer_addr) => {
                info!(
                    "{} Connection migrated to ({}, {})",
                    client.conn.trace_id(),
                    local_addr,
                    peer_addr
                );
            }

            quiche::PathEvent::PeerPathStatus(addr, path_status) => {
                info!("Peer asks status {:?} for {:?}", path_status, addr,);
                client
                    .conn
                    .set_path_status(addr.0, addr.1, path_status, false)
                    .expect("cannot follow status request");
            }
        }
    }
}

/// Generate a new pair of Source Connection ID and reset token.
fn generate_cid_and_reset_token<T: SecureRandom>(rng: &T) -> (quiche::ConnectionId<'static>, u128) {
    let mut scid = [0; quiche::MAX_CONN_ID_LEN];
    rng.fill(&mut scid).unwrap();
    let scid = scid.to_vec().into();
    let mut reset_token = [0; 16];
    rng.fill(&mut reset_token).unwrap();
    let reset_token = u128::from_be_bytes(reset_token);
    (scid, reset_token)
}

/// Makes a buffered writer for a qlog.
pub fn make_qlog_writer(
    dir: &std::ffi::OsStr,
    role: &str,
    id: &str,
) -> std::io::BufWriter<std::fs::File> {
    let mut path = std::path::PathBuf::from(dir);
    let filename = format!("{}-{}.sqlog", role, id);
    path.push(filename);

    match std::fs::File::create(&path) {
        Ok(f) => std::io::BufWriter::new(f),

        Err(e) => panic!(
            "Error creating qlog file attempted path was {:?}: {}",
            path, e
        ),
    }
}
