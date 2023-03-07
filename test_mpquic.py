import park
from park.utils.colorful_print import print_red

import random
import collections

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

learning_rate = 0.0005
gamma         = 0.98
buffer_limit  = 50000
batch_size    = 32

class ReplayBuffer():
    def __init__(self):
        self.buffer = collections.deque(maxlen=buffer_limit)
    
    def put(self, transition):
        self.buffer.append(transition)
    
    def sample(self, n):
        mini_batch = random.sample(self.buffer, n)
        s_lst, a_lst, r_lst, s_prime_lst, done_mask_lst = [], [], [], [], []
        
        for transition in mini_batch:
            s, a, r, s_prime, done_mask = transition
            s_lst.append(s)
            a_lst.append([a])
            r_lst.append([r])
            s_prime_lst.append(s_prime)
            done_mask_lst.append([done_mask])

        return torch.tensor(s_lst, dtype=torch.float), torch.tensor(a_lst), \
               torch.tensor(r_lst), torch.tensor(s_prime_lst, dtype=torch.float), \
               torch.tensor(done_mask_lst)
    
    def size(self):
        return len(self.buffer)

class Qnet(nn.Module):
    def __init__(self):
        super(Qnet, self).__init__()
        self.fc1 = nn.Linear(4, 128)
        self.fc2 = nn.Linear(128, 128)
        self.fc3 = nn.Linear(128, 2)

    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return x
      
    def sample_action(self, obs, epsilon):
        out = self.forward(obs)
        coin = random.random()
        if coin < epsilon:
            return random.randint(0,2)
        else : 
            return out.argmax().item()
            
def train(q, q_target, memory, optimizer):
    for i in range(10):
        s,a,r,s_prime,done_mask = memory.sample(batch_size)

        q_out = q(s)
        q_a = q_out.gather(1,a)
        max_q_prime = q_target(s_prime).max(1)[0].unsqueeze(1)
        target = r + gamma * max_q_prime * done_mask
        loss = F.smooth_l1_loss(q_a, target)
        
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

class RandomAgent(object):
    def __init__(self, state_space, action_space, *args, **kwargs):
        self.state_space = state_space
        self.action_space = action_space

        self.counter = 0
        self.learning_cycle = 100
        
        self.q = Qnet()
        self.q_target = Qnet()
        self.q_target.load_state_dict(self.q.state_dict())
        self.memory = ReplayBuffer()
        self.optimizer = optim.Adam(self.q.parameters(), lr=learning_rate)
        self.act = self.action_space.sample()
        self.obs = [0,0,0,0]
        self.epsilon = 0.08

    def get_action(self, obs, prev_reward, prev_done, prev_info):
        # act = self.action_space.sample()
        #print_red(f"get_action {obs} {prev_reward} {prev_done} {prev_info}")
        # implement real action logic here
        
        done_mask = 0.0 if prev_done else 1.0
        self.memory.put((self.obs,self.act,prev_reward,obs, done_mask))

        self.act = self.q.sample_action(torch.from_numpy(np.array(obs)).float(),self.epsilon)
        self.obs = obs
        #HABLAR CON JOSE, en el ejemplo de github se añade a la coleccion para entrenar la observacion,
        #la accion que se genera con esa observacion, la recompensa de esa sccion, la observacion que se genera con
        #esa accion y la done_mask, pero la recompensa y la observacion siguiente no las tenemos hasta el siguiente ciclo.
        
        self.counter+=1
        if self.counter == self.learning_cycle:
            self.counter = 0
            train(self.q, self.q_target, self.memory, self.optimizer)
        
        return self.act



def main():
    env = park.make('mpquic')
    agent = RandomAgent(env.observation_space, env.action_space)
    env.run(agent)

if __name__ == "__main__":
    main()