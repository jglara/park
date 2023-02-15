import park
from park import core, spaces, logger
from park.param import config
from park.utils import seeding
from park.utils.colorful_print import print_red
from park.spaces.box import Box
from park.spaces.discrete import Discrete

import numpy as np
from mininet.node import OVSBridge
from mininet.clean import cleanup
from mininet.net import Mininet


from mininet.topo import Topo
from mininet.cli import CLI
from mininet.net import Mininet
from mininet.node import OVSBridge, Host
from mininet.link import Link
from mininet.link import TCIntf
from mininet.log import setLogLevel, info, debug
from mininet.clean import cleanup
import threading




import capnp
capnp.remove_import_hook()
mpquic_capnp = capnp.load(park.__path__[0] + "/envs/mpquic/mpquic-quiche/src/data.capnp")

class SchedulerImpl(mpquic_capnp.Scheduler.Server):
    def __init__(self):
        self.rtts = []

    def nextPath(self, d, _context, **kwargs):
        logger.info("d.best_rtt = {} d.second_rtt = {}".format(d.bestRtt, d.secondRtt))
        self.rtts.append((d.bestRtt, d.secondRtt))
        return 0

def run_forever():    
    addr=("*:6677")
    logger.info("Starting communication with MPQUIC server")
    server = capnp.TwoPartyServer(addr, bootstrap=SchedulerImpl())
    server.run_forever()


class MyTCLink( Link ):
    "Link with symmetric TC interfaces configured via opts"
    def __init__( self, node1, node2, port1=None, port2=None,
                  intfName1=None, intfName2=None,
                  addr1=None, addr2=None, ip1=None, ip2=None, **params ):
        Link.__init__( self, node1, node2, port1=port1, port2=port2,
                       intfName1=intfName1, intfName2=intfName2,
                       cls1=TCIntf,
                       cls2=TCIntf,
                       addr1=addr1, addr2=addr2,
                       params1=params,
                       params2=params )
        if ip1 is not None:
            self.intf1.setIP(ip1)

        if ip2 is not None:
            self.intf2.setIP(ip2)


class Router( Host ):
    "A Node with forwarding on"
    def config( self, **params ):
        super( Router, self).config( **params )
        self.cmd("sysctl -w net.ipv4.ip_forward=1")


class MultiHost( Host ):
    "A Node wiht two interfaces"
    def config( self, **params ):
        super( MultiHost, self).config( **params )
        self.cmd("ip rule add from 10.0.1.1 table 1")
        self.cmd("ip route add 10.0.1.0/24 dev h1-eth1 scope link table 1")
        self.cmd("ip route add default via 10.0.1.10 dev h1-eth1 table 1")

        self.cmd("ip rule add from 10.0.2.1 table 2")
        self.cmd("ip route add 10.0.2.0/24 dev h1-eth2 scope link table 2")
        self.cmd("ip route add default via 10.0.2.10 dev h1-eth2  table 2")

class MultipathTopo( Topo ):
    LTE = "lte"
    WIFI = "wifi"
    def build(self, **opts):
        info("Topo params {}".format(opts))
        sw1 = self.addSwitch("sw1")
        sw2 = self.addSwitch("sw2")
        sw3 = self.addSwitch("sw3")
        host = self.addHost( 'h1', ip='10.0.1.1/24', cls=MultiHost)
        server = self.addHost( 's1', ip='10.0.3.10/24', cls=Host, defaultRoute='via 10.0.3.1', inNamespace=False)
        router = self.addHost( 'r1', ip='10.0.3.1/24', cls=Router)


        linkConfig_lte = opts[self.LTE]
        linkConfig_wifi = opts[self.WIFI]
        #linkConfig_server = {'bw': 50, 'delay': '5ms', 'loss': 0, 'jitter': 0, 'max_queue_size': 10000 }
#, 'txo': False, 'rxo': False

        # server router connections
        self.addLink( sw3, server, cls=MyTCLink, intfName2='s1-eth1', ip2='10.0.3.10/24')
        self.addLink( sw3, router, cls=MyTCLink, intfName2='r1-eth3', ip2='10.0.3.1/24')

        # client router connections
        self.addLink( sw1, host, cls=MyTCLink, intfName2='h1-eth1', ip2='10.0.1.1/24', **linkConfig_lte)
        self.addLink( sw1, router, cls=MyTCLink, intfName2='r1-eth1', ip2='10.0.1.10/24')

        self.addLink( sw2, host, cls=MyTCLink, intfName2='h1-eth2', ip2='10.0.2.1/24', **linkConfig_wifi)
        self.addLink( sw2, router, cls=MyTCLink, intfName2='r1-eth2', ip2='10.0.2.10/24')            
        


class QuicheQuic:
    NAME = "quichequic"    
    QUICHEPATH = park.__path__[0] + "/envs/mpquic/mpquic-quiche"
    
    def __init__(self, net, file_size, output_dir):
        self.file_path = "test.bin"
        self.file_size = file_size
        self.output_dir = output_dir
        self.net = net
        self.loglevel = 'info'

    def prepare(self):
        self.net.getNodeByName('s1').cmd("truncate -s {size} {path}".format(
            size= self.file_size, 
            path= self.file_path))
        
    def get_server_cmd(self):
        #QLOGDIR={csv_path} 
        cmd="RUST_LOG={loglevel} {quichepath}/target/debug/mp_server --listen 10.0.3.10:4433 --cert {quichepath}/src/bin/cert.crt --key {quichepath}/src/bin/cert.key --root {wwwpath} --scheduler rl > {output}/server.log&".format(            
            output=self.output_dir,            
            quichepath=self.QUICHEPATH, 
            wwwpath='./',
            loglevel=self.loglevel)

        logger.info(cmd)
        return cmd

    def get_client_cmd(self):

        cmd="RUST_LOG={loglevel} {quichepath}/target/debug/mp_client -l 10.0.1.1:5555 -w 10.0.2.1:6666 --url https://10.0.3.10:4433/{file} > {output}/client.log".format(            
            output=self.output_dir,        
            quichepath=self.QUICHEPATH, 
            file=self.file_path,
            loglevel=self.loglevel
            )
        
        logger.info(cmd)
        return cmd

    def clean(self):    
        self.net.getNodeByName('s1').cmd("rm {path}".format(
            path= self.file_path))
        

    def run(self):
        import time
        
        time.sleep(1)
        self.net.getNodeByName('s1').cmd(self.get_server_cmd())            
        self.net.getNodeByName('h1').cmd(self.get_client_cmd())


class MultipathQuicEnv(core.SysEnv):
    def __init__(self):
        # state_space
        # bestRtt  : 0-1000000 (ms)
        # secondRtt: 0-1000000 (ms)
        self.observation_space = Box(
            low=np.array([0] * 2),
            high=np.array([1e6, 1e6]),
            dtype="float32"
        )

        # action_space
        # Actions: 
        #   0 do not send
        #   1 send only best path
        #   2 send only second path
        #   3 send both
        self.action_space = Discrete(4) 

        self.output_dir = park.__path__[0] + "/../test_mpquic"
        self.topo_params = {
            'lte' : {
                "bw": 8.6,
                "delay": '10ms'
            },
            'wifi' : {
                "bw": 0.3,
                "delay": '100ms'
            }
        }

        # reset mininet environment
        cleanup()

        self.topo = MultipathTopo(**self.topo_params)
        self.net = Mininet(self.topo, switch=OVSBridge, controller=None)
        self.net.start()

    def run(self, agent):
        logger.info("Setup agent")


        # start rpc server                 
        t = threading.Thread(target=run_forever)
        t.daemon = True
        t.start()

        # Start
        self.reset()

    def reset(self):
        logger.info("Start download")

        # run experiment
        exp = QuicheQuic(file_size='1M', net=self.net, output_dir=self.output_dir)
        exp.prepare()
        exp.run()
        exp.clean()

        cleanup()


        
