import traceback
import park
from park import core, spaces, logger
from park.param import config
from park.utils import seeding
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
import time


import capnp
capnp.remove_import_hook()
mpquic_capnp = capnp.load(
    park.__path__[0] + "/envs/mpquic/mpquic-quiche/src/data.capnp")


class SchedulerImpl(mpquic_capnp.Scheduler.Server):
    def __init__(self, agent):
        # self.rtts = []
        self.agent = agent
        self.prevObs = [0, 0, 0, 0]
        self.A = 1
        self.B = 1
        self.prevTime = time.time()

    def reward(self, obs):
        # TODO: define reward and info
        bestDeltaRtt = obs[0] - self.prevObs[0]
        secondDeltaRtt = obs[1] - self.prevObs[1]

        reward = self.A*(obs[2]+obs[3]) - self.B * \
            np.log(max(bestDeltaRtt+secondDeltaRtt, 0.00001))

        return (reward, None)

    def nextPath(self, d, _context, **kwargs):
        logger.info("d.best_rtt = {} d.second_rtt = {}".format(d.bestRtt, d.secondRtt))
        # self.rtts.append((d.bestRtt, d.secondRtt))

        newTime = time.time()
        elapsed = (newTime - self.prevTime)*1000
        bestThrough = d.bestAcked / elapsed
        secondThrough = d.secondAcked / elapsed

        obs = [d.bestRtt, d.secondRtt, bestThrough, secondThrough]

        reward, info = self.reward(obs)
        act = self.agent.get_action(obs, reward, False, info)

        self.prevObs = obs.copy()
        self.prevTime = newTime

        return act


def run_forever(addr, agent):
    logger.info("Starting communication with MPQUIC server")
    try:
        server = capnp.TwoPartyServer(addr, bootstrap=SchedulerImpl(agent))
        server.run_forever()
    except Exception:
        print(traceback.format_exc())



class ProxyAgent(object):
    def __init__(self):
        self.agent = None

    def set_agent(self, agent):
        self.agent = agent

    def get_action(self, *args):
        assert (self.agent != None)
        return self.agent.get_action(*args)


class Agent(object):
    def __init__(self,  *args, **kwargs):
        pass

    def get_action(self, obs, prev_reward, prev_done, prev_info):
        act = 0
        # implement real action logic here
        return act


class MyTCLink(Link):
    "Link with symmetric TC interfaces configured via opts"

    def __init__(self, node1, node2, port1=None, port2=None,
                 intfName1=None, intfName2=None,
                 addr1=None, addr2=None, ip1=None, ip2=None, **params):
        Link.__init__(self, node1, node2, port1=port1, port2=port2,
                      intfName1=intfName1, intfName2=intfName2,
                      cls1=TCIntf,
                      cls2=TCIntf,
                      addr1=addr1, addr2=addr2,
                      params1=params,
                      params2=params)
        if ip1 is not None:
            self.intf1.setIP(ip1)

        if ip2 is not None:
            self.intf2.setIP(ip2)


class Router(Host):
    "A Node with forwarding on"

    def config(self, **params):
        super(Router, self).config(**params)
        self.cmd("sysctl -w net.ipv4.ip_forward=1")


class MultiHost(Host):
    "A Node wiht two interfaces"

    def config(self, **params):
        super(MultiHost, self).config(**params)
        self.cmd("ip rule add from 10.0.1.1 table 1")
        self.cmd("ip route add 10.0.1.0/24 dev h1-eth1 scope link table 1")
        self.cmd("ip route add default via 10.0.1.10 dev h1-eth1 table 1")

        self.cmd("ip rule add from 10.0.2.1 table 2")
        self.cmd("ip route add 10.0.2.0/24 dev h1-eth2 scope link table 2")
        self.cmd("ip route add default via 10.0.2.10 dev h1-eth2  table 2")


class MultipathTopo(Topo):
    LTE = "lte"
    WIFI = "wifi"

    def build(self, **opts):
        info("Topo params {}".format(opts))
        sw1 = self.addSwitch("sw1")
        sw2 = self.addSwitch("sw2")
        sw3 = self.addSwitch("sw3")
        host = self.addHost('h1', ip='10.0.1.1/24', cls=MultiHost)
        server = self.addHost('s1', ip='10.0.3.10/24', cls=Host,
                              defaultRoute='via 10.0.3.1', inNamespace=False)
        router = self.addHost('r1', ip='10.0.3.1/24', cls=Router)

        linkConfig_lte = opts[self.LTE]
        linkConfig_wifi = opts[self.WIFI]
        # linkConfig_server = {'bw': 50, 'delay': '5ms', 'loss': 0, 'jitter': 0, 'max_queue_size': 10000 }
# , 'txo': False, 'rxo': False

        # server router connections
        self.addLink(sw3, server, cls=MyTCLink,
                     intfName2='s1-eth1', ip2='10.0.3.10/24')
        self.addLink(sw3, router, cls=MyTCLink,
                     intfName2='r1-eth3', ip2='10.0.3.1/24')

        # client router connections
        self.addLink(sw1, host, cls=MyTCLink, intfName2='h1-eth1',
                     ip2='10.0.1.1/24', **linkConfig_lte)
        self.addLink(sw1, router, cls=MyTCLink,
                     intfName2='r1-eth1', ip2='10.0.1.10/24')

        self.addLink(sw2, host, cls=MyTCLink, intfName2='h1-eth2',
                     ip2='10.0.2.1/24', **linkConfig_wifi)
        self.addLink(sw2, router, cls=MyTCLink,
                     intfName2='r1-eth2', ip2='10.0.2.10/24')


class QuicheQuic:
    NAME = "quichequic"
    QUICHEPATH = park.__path__[0] + "/envs/mpquic/mpquic-quiche"    

    def __init__(self, net, file_size, output_dir):
        self.file_path = "test.bin"
        self.file_size = file_size        
        self.net = net

    def prepare(self):
        self.net.getNodeByName('s1').cmd("truncate -s {size} {path}".format(
            size=self.file_size,
            path=self.file_path))

        self.net.getNodeByName('s1').cmd(self.get_server_cmd())

    def get_server_cmd(self):
        # QLOGDIR={csv_path}
        cmd = "{quichepath}/target/debug/mp_server --listen 10.0.3.10:4433 --cert {quichepath}/src/bin/cert.crt --key {quichepath}/src/bin/cert.key --root {wwwpath} --scheduler minRtt --logging-config {log}&".format(            
            quichepath=self.QUICHEPATH,
            wwwpath='./',
            log = park.__path__[0] + "/../server_log.yaml"
            )

        logger.info(cmd)
        return cmd

    def get_client_cmd(self):

        cmd = "{quichepath}/target/debug/mp_client -l 10.0.1.1:5555 -w 10.0.2.1:6666 --url https://10.0.3.10:4433/{file} --logging-config {log}".format(            
            quichepath=self.QUICHEPATH,
            file=self.file_path,
            log = park.__path__[0] + "/../client_log.yaml"
        )

        logger.info(cmd)
        return cmd

    def clean(self):
        self.net.getNodeByName('s1').cmd("rm {path}".format(
            path=self.file_path))

    def run(self):
        self.net.getNodeByName('h1').cmd(self.get_client_cmd())




if __name__ == "__main__":
    output_dir = park.__path__[0] + "/../test_mpquic"
    topo_params = {
            'lte': {
                "bw": 8.6,
                "delay": '10ms'
            },
            'wifi': {
                "bw": 0.3,
                "delay": '100ms'
            }
        }

    # reset mininet environment
    cleanup()

    topo = MultipathTopo(**topo_params)
    net = Mininet(topo, switch=OVSBridge, controller=None)
    net.start()

    # start rpc server
    global proxy_agent
    proxy_agent = ProxyAgent()
    agent = Agent()
    proxy_agent.set_agent(agent)
    t = threading.Thread(target=run_forever, args=("*:6677", proxy_agent))
    t.daemon = True
    t.start()

    exp = QuicheQuic(file_size='1M', net=net,
                        output_dir=output_dir)
    exp.prepare()

    print(exp.get_client_cmd())

    CLI(net)






