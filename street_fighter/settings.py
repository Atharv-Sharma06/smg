"""Game-wide settings: window size, physics, round rules and colours."""

TITLE = "Street Clash"
WIDTH = 1000
HEIGHT = 600
FPS = 60

# Arena / physics
GROUND_Y = 515            # y position of a fighter's feet when standing on the floor
GRAVITY = 0.85
FIGHTER_SCALE = 1.25      # how big the fighters are drawn during a fight
STAGE_LEFT = 45           # how far left / right the fighters can walk
STAGE_RIGHT = WIDTH - 45

# Rounds
ROUND_TIME = 60           # seconds per round
ROUNDS_TO_WIN = 2         # best of three
MAX_ROUNDS = 5            # after this many rounds a tied match is called a draw

# Energy (spent on powers)
MAX_ENERGY = 100
START_ENERGY = 30
ENERGY_REGEN = 0.1        # per frame, so 6 energy per second

# Colours
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GREY = (150, 150, 165)
YELLOW = (255, 215, 50)
RED = (235, 50, 50)
PLAYER_COLORS = {1: (80, 160, 255), 2: (255, 85, 85)}
