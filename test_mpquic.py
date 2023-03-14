import park
from park.utils.colorful_print import print_red

import random
import collections
import time
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
            return random.randint(0,1)
        else : 
            return out.argmax().item()
            
def train(q, q_target, memory, optimizer):
    for i in range(2):
        # park.logger.info("Training iteration {} start".format(i))
        now =  time.time()
        s,a,r,s_prime,done_mask = memory.sample(batch_size)
        # park.logger.info("Training iteration {} sample obtained".format(i))
        q_out = q(s)
        # park.logger.info("Training iteration {} q".format(i))
        q_a = q_out.gather(1,a)
        # park.logger.info("Training iteration {} forward done".format(i))
        max_q_prime = q_target(s_prime).max(1)[0].unsqueeze(1)
        target = r + gamma * max_q_prime * done_mask
        # park.logger.info("Training iteration {} target calculated".format(i))
        loss = F.smooth_l1_loss(q_a, target)
        # park.logger.info("Training iteration {} loss calculated".format(i))
        
        optimizer.zero_grad()
        # park.logger.info("Training iteration {} gradient to zero".format(i))
        loss.backward()
        # park.logger.info("Training iteration {} backpropagation completed".format(i))
        optimizer.step()
        # park.logger.info("Training iteration {} ended in {}".format(i,time.time()-now))

class RandomAgent(object):
    def __init__(self, state_space, action_space, *args, **kwargs):
        self.state_space = state_space
        self.action_space = action_space

        self.learning_counter = 0
        self.learning_cycle = 50
        
        self.q = Qnet()
        self.q_target = Qnet()
        self.q_target.load_state_dict(self.q.state_dict())
        self.memory = ReplayBuffer()
        self.optimizer = optim.Adam(self.q.parameters(), lr=learning_rate)
        self.act = self.action_space.sample()
        self.obs = [0,0,0,0]
        self.epsilon = 0.08

        self.update_counter = 0
        self.update_cycle = 5

        self. score = 0
    def update_epsilon(self,epoch,max_epoch):
        self.epsilon = max(0.01, 0.08 - 0.01*(epoch/(max_epoch/50)))
    def get_action(self, obs, prev_reward, prev_done, prev_info):
        # act = self.action_space.sample()
        #print_red(f"get_action {obs} {prev_reward} {prev_done} {prev_info}")
        # implement real action logic here
        
        done_mask = 0.0 if prev_done else 1.0
        self.memory.put((self.obs,self.act,prev_reward,obs, done_mask))

        self.act = self.q.sample_action(torch.from_numpy(np.array(obs)).float(),self.epsilon)
        self.obs = obs
        
        self.learning_counter+=1
        self.score += prev_reward
        if self.learning_counter == self.learning_cycle:
            self.learning_counter = 0
            # park.logger.info("Starting training")
            now = time.time()
            train(self.q, self.q_target, self.memory, self.optimizer)
            # park.logger.info("Training time {}".format(time.time()-now))
            self.update_counter +=1
        if self.update_counter == self.update_cycle:
            # park.logger.info("a")
            self.update_counter = 0
            self.q_target.load_state_dict(self.q.state_dict())
            park.logger.info("score : {:.1f}, n_buffer : {}, eps : {:.1f}%".format(
                                                            self.score/(self.learning_cycle*self.update_cycle), self.memory.size(), self.epsilon*100))
        return self.act



def main():
    env = park.make('mpquic')
    agent = RandomAgent(env.observation_space, env.action_space)
    env.run(agent)
    agent.update_epsilon(1,20)
    env.reset()
    # for i in range(20):
    #     agent.update_epsilon(i,20)
    #     env.reset()
if __name__ == "__main__":
    main()