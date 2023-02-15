import park
from park.utils.colorful_print import print_red

class RandomAgent(object):
    def __init__(self, state_space, action_space, *args, **kwargs):
        self.state_space = state_space
        self.action_space = action_space

    def get_action(self, obs, prev_reward, prev_done, prev_info):
        #act = self.action_space.sample()
        #print_red(f"get_action {obs} {prev_reward} {prev_done} {prev_info}")
        # implement real action logic here
        return 0

def main():
    env = park.make('mpquic')
    agent = RandomAgent(env.observation_space, env.action_space)
    env.run(agent)

if __name__ == "__main__":
    main()