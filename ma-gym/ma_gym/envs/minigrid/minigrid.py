import copy
from ctypes.wintypes import CHAR
import logging
from pickle import LONG4
import gym
import os
import torch as T
import numpy as np
from PIL import ImageColor
from gym import spaces
from gym.utils import seeding
from ..utils.action_space import MultiAgentActionSpace
from ..utils.draw import draw_grid, fill_cell, draw_circle, write_cell_text, draw_score_board
from ..utils.observation_space import MultiAgentObservationSpace
from ..utils.replay_buffer import ReplayBuffer
from ..utils.deep_q_network import DeepQNetwork

"""
Minigrid Environment for Automata Project with multi-agent systems.

LIMITATIONS OF AGENT/S:



Each agent's observation includes its:
    - Agent ID 
    - Position within the grid
    - Number of steps since beginning
    - Full Observability of the environment

------------------------------------------------------------------------------------------------------------------------------

ACTION SPACE:
------------------------------------------------------------------------------------------------------------------------------

{move forward, rotate 90 right, rotate 90 left, break, craft }

Shortened to:
{F, R, L, B, C}

------------------------------------------------------------------------------------------------------------------------------

Arguments:
    grid_shape: size of the grid
    n_agents: 
    n_rocks:
    n_fires:

    agent_view: size of the agent view range in each direction
    full_observable: flag whether agents should receive observation for all other agents

Attributes:
    _agent_dones: list with indicater whether the agent is done or not.
    _base_img: base image with grid
    _viewer: viewer for the rendered image
"""


class Minigrid(gym.Env):

    metadata = {'render.modes': ['human', 'rgb_array']} #must be human so it is readable data by humans

    def __init__(self, grid_shape=(10, 10), n_agents=4, n_rocks=1, n_fires = 1, full_observable=True, max_steps=300, 
                 agent_view_mask=(9, 9)):
        
        assert len(grid_shape) == 2, 'expected a tuple of size 2 for grid_shape, but found {}'.format(grid_shape)
        assert len(agent_view_mask) == 2, 'expected a tuple of size 2 for agent view mask,' \
                                          ' but found {}'.format(agent_view_mask)
        assert grid_shape[0] > 0 and grid_shape[1] > 0, 'grid shape should be > 0'
        assert 0 < agent_view_mask[0] <= grid_shape[0], 'agent view mask has to be within (0,{}]'.format(grid_shape[0])
        assert 0 < agent_view_mask[1] <= grid_shape[1], 'agent view mask has to be within (0,{}]'.format(grid_shape[1])
        #############   PARAMS #################
        self.alpha          = 0.001 
        self.beta           = 0.002
        self.gamma          = 0.98 
        self.epsilon        = 1
        self.eps_min        = 0.01
        self.eps_dec        = 1e-5
        self.lr             = 0.001
        self.input_dims     = [105]
        self.n_actions      = 5
        self.mem_size       = 10000
        self.batch_size     = 64
        self.mem_cntr       = 0
        self.replace        = 1000
        self.iter_cntr      = 0

        self.learn_step_counter = 0
        self.replace_target_cnt = self.replace
        self.observation_cntr   = 0
        self.rocks_delay = 0

        self.env_name       = "minigrid"
        self.algo           = ""
        self.checkpoint_dir = 'examples/checkpoints'

        ############# PARAMS #################
        self._base_grid_shape = grid_shape
        self._agent_grid_shape = agent_view_mask

        self.n_agents = n_agents
        self.n_rocks = n_rocks
        self.n_fires = n_fires
        
        self._max_steps = max_steps
        self._step_count = None
        self._agent_view_mask = agent_view_mask
        self.agent_reward = 0
        self.agent_action = None


        self.AGENT_COLOR = ImageColor.getcolor("green", mode='RGB')
        self.OBSERVATION_VISUAL_COLOR = ImageColor.getcolor("gray", mode='RGB')
        self.ROCK_COLOR = ImageColor.getcolor("gray", mode='RGB')
        self.FIRE_COLOR = ImageColor.getcolor("red", mode='RGB')

        #Inital position Initilization for each object
        self._init_agent_pos = {_: None for _ in range(self.n_agents)}
        self._init_rock_pos = {_: None for _ in range(self.n_rocks)}
        self._init_fire_pos = {_: None for _ in range(self.n_fires)}

        self.action_space = MultiAgentActionSpace([spaces.Discrete(5) for _ in range(self.n_agents)])

        self.agent_pos = {_: None for _ in range(self.n_agents)}
        self.rocks_pos = {_: None for _ in range(self.n_rocks)}
        self.fire_pos  = {_: None for _ in range(self.n_fires)}

        self._base_grid = self.__create_grid() 
        self._full_obs = self.__create_grid()
        self._agent_dones = [False for _ in range(self.n_agents)]
 
        self.viewer = None
        self.full_observable = full_observable

        mask_size = np.prod(self._agent_view_mask)
        self._obs_high = np.array([1., 1.] + [1.] * mask_size + [1.0], dtype=np.float32)
        self._obs_low  = np.array([0., 0.] + [0.] * mask_size + [0.0], dtype=np.float32)
        if self.full_observable:
            self._obs_high = np.tile(self._obs_high, self.n_agents)
            self._obs_low = np.tile(self._obs_low, self.n_agents)
        self.observation_space = MultiAgentObservationSpace(
            [spaces.Box(self._obs_low, self._obs_high) for _ in range(self.n_agents)])

        self._total_episode_reward = None
        self.seed()
    """
        Function: get_action_meanings()
        Inputs  : agent_i 
        Outputs : None
        Purpose : Reports back what move agent/user chose
    """
    def get_action_meanings(self, agent_i=None):
        if agent_i is not None:
            assert agent_i <= self.n_agents
            return [ACTION_MEANING[i] for i in range(self.action_space[agent_i].n)]
        else:
            return [[ACTION_MEANING[i] for i in range(ac.n)] for ac in self.action_space]

    def sample_action_space(self): # only for eps-greedy action  
        return [agent_action_space.sample() for agent_action_space in self.action_space]

    def __draw_base_img(self): # draws empty grid
        self._base_img = draw_grid(self._base_grid_shape[0], self._base_grid_shape[1], cell_size=CELL_SIZE, fill='white')

    def __create_grid(self):
        _grid = [[PRE_IDS['empty'] for _ in range(self._base_grid_shape[1])] for row in range(self._base_grid_shape[0])]
        return _grid

    def __init_map(self):
        self._full_obs = self.__create_grid()

        for agent_i in range(self.n_agents):
            while True:
                pos = [self.np_random.randint(0, self._base_grid_shape[0] - 1), 
                       self.np_random.randint(0, self._base_grid_shape[1] - 1)]
                if self._is_cell_vacant(pos):
                    self.agent_pos[agent_i] = pos
                    self._init_agent_pos = pos
                    break
            self.__update_agent_view(agent_i)
        
     
        for rock_i in range(self.n_rocks):
            while True:
                pos = [self.np_random.randint(0, self._base_grid_shape[0] - 1),
                       self.np_random.randint(0, self._base_grid_shape[1] - 1)]
                if self._is_cell_vacant(pos) and (self._neighbor_agents(pos)[0] == 0):
                    self.rock_pos[rock_i] = pos
                    self._init_rock_pos = pos
                break
            self.__update_rock_view(rock_i)

        for fire_i in range(self.n_fires):
            while True:
                pos = [self.np_random.randint(0, self._base_grid_shape[0] - 1),
                       self.np_random.randint(0, self._base_grid_shape[1] - 1)]
                if self._is_cell_vacant(pos):
                    self.fire_pos[fire_i] = pos
                    self._init_fire_pos = pos
                break
            self.__update_fire_view(fire_i)

        self.__draw_base_img()
    """
        Function : get_agent_obs()
        Inputs   : None
        Outputs  : full observation of the grid world
        Purpose  : 
    """
    def get_agent_obs(self):
        _obs = []
        for agent_i in range(self.n_agents):
            pos = self.agent_pos[agent_i]
            # print("Agent Pos: {}".format(pos))

            _agent_i_obs = [pos[0] / (self._agent_grid_shape[0] - 1), pos[1] / (self._agent_grid_shape[1] - 1)]  #coordinate of agent

            # check if rock is in the view area and give it future (time+1) rock coordinates
            _rock_pos = np.zeros(self._agent_view_mask)  
            _fire_pos = np.zeros(self._agent_view_mask) # rock location in neighbor
            for row in range(max(0, pos[0] - 2), min(pos[0] + 2 + 1, self._agent_grid_shape[0] - 2)):
                for col in range(max(0, pos[1] - 2), min(pos[1] + 2 + 1, self._agent_grid_shape[1] - 2)):
                    if PRE_IDS['rock'] in self._full_obs[row][col]:
                        _rock_pos[row - (pos[0] - 2), col - (pos[1] - 2)] = 1  
                    # if PRE_IDS['fire'] in self._full_obs[row][col]:
                    #     _fire_pos[row - (pos[0] - 2), col - (pos[1] - 2)] = 1
                        #print("Observation space \n {}".format(_rock_pos))
            _agent_i_obs += _rock_pos.flatten().tolist()  # adding rock pos in observable area
            _agent_i_obs += _fire_pos.flatten().tolist()
            _agent_i_obs += [self._step_count / self._max_steps]  # adding the time

            _obs.append(_agent_i_obs)

        if self.full_observable:
            _obs = np.array(_obs).flatten().tolist() # flatten to np array 
            _obs = [_obs for _ in range(self.n_agents)]
        return _obs

    def reset(self):
        self._total_episode_reward = [0 for _ in range(self.n_agents)]
        self.agent_pos = {}
        self.rock_pos = {}
        self.fire_pos = {} 
    
        self.__init_map()
        

        self._step_count = 0
        self._agent_dones = [False for _ in range(self.n_agents)]

        return self.get_agent_obs()

    def is_valid(self, pos):
        return (0 <= pos[0] < self._agent_grid_shape[0]) and (0 <= pos[1] < self._agent_grid_shape[1])

    def _is_cell_vacant(self, pos):
        return self.is_valid(pos) and (self._full_obs[pos[0]][pos[1]] == PRE_IDS['empty']) 

    def __update_agent_pos(self, agent_i, move):
        curr_pos = copy.copy(self.agent_pos[agent_i])
        next_pos = None

        moves = {
            0: [curr_pos[0], curr_pos[1]],     # Observe
            1: [curr_pos[0], curr_pos[1] - 1], # move left
            2: [curr_pos[0], curr_pos[1] + 1], # move right
        }

        next_pos = moves[move]

        if next_pos is not None and self._is_cell_vacant(next_pos):
            self.agent_pos[agent_i] = next_pos
            self._full_obs[curr_pos[0]][curr_pos[1]] = PRE_IDS['empty']
            self.__update_resources(agent_i,move)
   
            self.__update_agent_view(agent_i)
            self.update_agent_color([move])

    def update_rock_pos(self,rock_i):
        return
    
    def update_fire_pos(self,fire_i):
        return

    def __update_resources(self,agent_i,action):
        return


    def __update_agent_view(self, agent_i):
        self._full_obs[self.agent_pos[agent_i][0]][self.agent_pos[agent_i][1]] = PRE_IDS['agent'] + str(agent_i + 1)

    def __update_rock_view(self, rock_i):
        self._full_obs[self.rock_pos[rock_i][0]][self.rock_pos[rock_i][1]] = PRE_IDS['rock'] + str(rock_i + 1)

    def __update_fire_view(self, fire_i):
        self._full_obs[self.fire_pos[fire_i][0]][self.fire_pos[fire_i][1]] = PRE_IDS['fire'] + str(fire_i + 1)


    """
        Function : _neighbor_agents
        Inputs   : pos
        Outputs  : number of neighbors
        Purpose  : This function is the visual of the "observation space" for each agent. This is done by putting all the squares surrounding the agent as a "neighbor" and acts like an extension of the agent. 
    """
    def _neighbor_agents(self, pos): # this is really just the observable space   
        _count = 0
        neighbors_xy = []

        #Covers North, South, East, West
        if self.is_valid([pos[0] + 1, pos[1]]) and PRE_IDS['agent'] in self._full_obs[pos[0] + 1][pos[1]]:
            _count += 1
            neighbors_xy.append([pos[0] + 1, pos[1]])
        if self.is_valid([pos[0] - 1, pos[1]]) and PRE_IDS['agent'] in self._full_obs[pos[0] - 1][pos[1]]:
            _count += 1
            neighbors_xy.append([pos[0] - 1, pos[1]])
        if self.is_valid([pos[0], pos[1] + 1]) and PRE_IDS['agent'] in self._full_obs[pos[0]][pos[1] + 1]:
            _count += 1
            neighbors_xy.append([pos[0], pos[1] + 1])
        if self.is_valid([pos[0], pos[1] - 1]) and PRE_IDS['agent'] in self._full_obs[pos[0]][pos[1] - 1]:
            neighbors_xy.append([pos[0], pos[1] - 1])
            _count += 1

        # #Covers NE, SE, NW, SW
        # if self.is_valid([pos[0] + 1, pos[1] +1 ]) and PRE_IDS['agent'] in self._full_obs[pos[0] + 1][pos[1] + 1]:
        #     _count += 1
        #     neighbors_xy.append([pos[0] + 1, pos[1] + 1])
        # if self.is_valid([pos[0] - 1, pos[1] - 1 ]) and PRE_IDS['agent'] in self._full_obs[pos[0] - 1][pos[1] - 1]:
        #     _count += 1
        #     neighbors_xy.append([pos[0] - 1, pos[1] - 1])
        # if self.is_valid([pos[0] - 1, pos[1] + 1]) and PRE_IDS['agent'] in self._full_obs[pos[0] - 1][pos[1] + 1]:
        #     _count += 1
        #     neighbors_xy.append([pos[0] - 1, pos[1] + 1])
        # if self.is_valid([pos[0] + 1, pos[1] - 1]) and PRE_IDS['agent'] in self._full_obs[pos[0] + 1][pos[1] - 1]:
        #     neighbors_xy.append([pos[0] + 1, pos[1] - 1])
        #     _count += 1

        agent_id = []
        for x, y in neighbors_xy:
            agent_id.append(int(self._full_obs[x][y].split(PRE_IDS['agent'])[1]) - 1)
        return _count, agent_id
    """
        Function : step
        Purpose  : This function returns the rewards, state, and terminal conditions for each action decision
    """   
    def step(self, agents_action):
        if (self._step_count >= self._max_steps):
            for i in range(self.n_agents):
                self._agent_dones[i] = True

        for agent_i in range(self.n_agents):
            if(self.agent_pos[0][1] >= 9):
                self._agent_dones[agent_i] = True

        rewards = [0 for _ in range(self.n_agents)]
        in_range = 0
        _reward = 0
    

        for agent_i, action in enumerate(agents_action):
            if not (self._agent_dones[agent_i]):
                self.__update_agent_pos(agent_i, action)

        ###### INSERT REWARD FUNCTION HERE ###########


        ###### INSERT REWARD FUNCTION HERE ###########


        for i in range(self.n_agents):
            self._total_episode_reward[i] += rewards[i]

        self._step_count += 1
        
        self.agent_reward += rewards[0]
        self.agent_action = agents_action[0]


        #update rock  and fire positions for environment
        for rock_i in range(self.n_rocks):
            self.update_rock_pos(rock_i)

        for fire_i in range(self.n_fires):
            self.update_fire_pos(fire_i)
         
        print(self._full_obs)
        print(self.get_agent_obs())
        return self.get_agent_obs(), rewards, self._agent_dones, self.observation_cntr

    def __get_neighbor_coordinates(self, pos): #figures out the pos of each block of obs space

        #Covers North, South, East, West
        neighbors = []
        if self.is_valid([pos[0] + 1, pos[1]]):
            neighbors.append([pos[0] + 1, pos[1]])
        if self.is_valid([pos[0] - 1, pos[1]]):
            neighbors.append([pos[0] - 1, pos[1]])
        if self.is_valid([pos[0], pos[1] + 1]):
            neighbors.append([pos[0], pos[1] + 1])
        if self.is_valid([pos[0], pos[1] - 1]):
            neighbors.append([pos[0], pos[1] - 1])

        # #Covers NE, SE, NW, SW
        # if self.is_valid([pos[0] + 1, pos[1] +1 ]):
        #     neighbors.append([pos[0] + 1, pos[1] +1 ])
        # if self.is_valid([pos[0] - 1, pos[1] - 1]):
        #     neighbors.append([pos[0] - 1, pos[1] - 1])
        # if self.is_valid([pos[0] - 1, pos[1] + 1]):
        #     neighbors.append([pos[0] - 1, pos[1] + 1 ])
        # if self.is_valid([pos[0] + 1, pos[1] - 1]):
        #     neighbors.append([pos[0] + 1, pos[1] - 1])
     
        return neighbors

    def render(self, mode='human'): #renders the entire grid world as one frame
        
        img = copy.copy(self._base_img)

        #agent visual
        for agent_i in range(self.n_agents):
            draw_circle(img, self.agent_pos[agent_i], cell_size=CELL_SIZE, fill=self.AGENT_COLOR)
            write_cell_text(img, text=str(agent_i + 1), pos=self.agent_pos[agent_i], cell_size=CELL_SIZE,
                            fill='white', margin=0.4)
        #rock visual
        for rock_i in range(self.n_rocks):
            draw_circle(img, self.rock_pos[rock_i], cell_size=CELL_SIZE, fill=self.ROCK_COLOR)   
            write_cell_text(img, text=str(rock_i + 1), pos=self.rock_pos[rock_i], cell_size=CELL_SIZE,
                            fill='white', margin=0.4)
            
        for fire_i in range(self.n_fires):
            draw_circle(img, self.fire_pos[fire_i], cell_size=CELL_SIZE, fill=self.FIRE_COLOR)   
            write_cell_text(img, text=str(fire_i + 1), pos=self.fire_pos[fire_i], cell_size=CELL_SIZE,
                            fill='white', margin=0.4)

        img = draw_score_board(img,[self.agent_reward,self.agent_action])
        img = np.asarray(img)
        if mode == 'rgb_array':
            return img
        elif mode == 'human':
            from gym.envs.classic_control import rendering
            if self.viewer is None:
                self.viewer = rendering.SimpleImageViewer()
            self.viewer.imshow(img)
            return self.viewer.isopen

    def seed(self, n=None):
        self.np_random, seed = seeding.np_random(n)
        return [seed]

    def close(self):
        if self.viewer is not None:
            self.viewer.close()
            self.viewer = None

    def update_agent_color(self,action):
        action = action[0]
        if(action == OBSERVE_ID):
            self.AGENT_COLOR = ImageColor.getcolor("purple", mode='RGB')
            self.OBSERVATION_VISUAL_COLOR = ImageColor.getcolor("purple", mode='RGB')
        if(action == LEFT_ID):
            self.AGENT_COLOR = ImageColor.getcolor("red", mode='RGB')
            self.OBSERVATION_VISUAL_COLOR = ImageColor.getcolor("red", mode='RGB')
        if(action == RIGHT_ID):
            self.AGENT_COLOR = ImageColor.getcolor("orange", mode='RGB')
            self.OBSERVATION_VISUAL_COLOR = ImageColor.getcolor("orange", mode='RGB')
        if(action == BREAK_ID):
            self.AGENT_COLOR = ImageColor.getcolor("black", mode='RGB')
            self.OBSERVATION_VISUAL_COLOR = ImageColor.getcolor("black", mode='RGB')
        if(action == CRAFT_ID):
            self.AGENT_COLOR = ImageColor.getcolor("green", mode='RGB')
            self.OBSERVATION_VISUAL_COLOR = ImageColor.getcolor("green", mode='RGB')
            
    def save_models(self):
        self.q_eval.save_checkpoint()
        # self.q_next.save_checkpoint()

    def load_models(self):
        self.q_eval.load_checkpoint()
        # self.q_next.load_checkpoint()

    def remember(self, state, action, reward, new_state, done):
        self.memory.store_transition(state, action, reward, new_state, done)

    def choose_action(self, observation, evaluate=False): #epsilon-greedy action selection
        if np.random.random() > self.epsilon:
            state_i = T.tensor(observation[0],dtype=T.float).to(self.q_eval.device)
            actions = self.q_eval.forward(state_i)
            action = T.argmax(actions).item() # π∗(s) = argamax​ Q∗(s,a)
            action = [action]
        else:
            action = self.sample_action_space()
        return action

    def decrement_epsilon(self):
        self.epsilon = self.epsilon - self.eps_dec \
                        if self.epsilon > self.eps_min else self.eps_min
    def learn(self):

        if self.memory.mem_cntr < self.batch_size:
            return

        self.q_eval.optimizer.zero_grad()

        state, action, reward, new_state, done, batch = \
                                self.memory.sample_buffer(self.batch_size) # sample batch from experience

        state = T.tensor(state).to(self.q_eval.device)
        new_state = T.tensor(new_state).to(self.q_eval.device)
        action = action
        rewards = T.tensor(reward).to(self.q_eval.device)
        dones = T.tensor(done).to(self.q_eval.device)
        batch_index = np.arange(self.batch_size, dtype=np.int32)

        q_eval = self.q_eval.forward(state)[batch_index,action] #Q_t
        q_next = self.q_eval.forward(new_state)  # Q_t+1
        q_next[dones] = 0.0

        q_target = rewards + self.gamma*T.max(q_next, dim=1)[0] # Qπ(s,a) = r + γQπ(s′,π(s′))
        q_target = q_target.float() # turn to float to match datatypes of all tensors


        loss = self.q_eval.loss(q_target, q_eval).to(self.q_eval.device) # Mean Squared Error Loss
        loss.backward()
        
        self.q_eval.optimizer.step()

        self.iter_cntr += 1
        self.epsilon = self.epsilon - self.eps_dec \
            if self.epsilon > self.eps_min else self.eps_min #explore vs exploit


logger = logging.getLogger(__name__)

CELL_SIZE = 35
WALL_COLOR = 'white'

ACTION_MEANING = {
    0: "O", # Make purple

    1: "L", # Make red
    2: "R", # Make orange

    3: "B", # Make black
    4: "C", # Make green
}

#sep. actions for observing and slewing 
PRE_IDS = {
    'agent': 'A',
    'rock': 'R',
    'wall': 'W',
    'empty': '0',
    'fire' : 'F'
}

OBSERVE_ID = 0
LEFT_ID = 1
RIGHT_ID = 2
BREAK_ID = 3
CRAFT_ID = 4
