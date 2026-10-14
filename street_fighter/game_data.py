"""
Everything the players can choose lives in plain dictionaries:

    CHARACTERS     - the fighters, their stats and colours
    POWERS         - the special powers, what they do and what they cost
    ATTACKS        - timing data for punches, kicks and the attacking powers
    CONTROLS       - which keys each player uses
    CONTROL_HINTS  - the same controls written out for the menus

Add a new entry to CHARACTERS or POWERS and it appears on the select screen
automatically. A new power has to use one of the existing "type"s:
projectile, strike, uppercut, dash, heal, shield or rage.
"""
import pygame

CHARACTERS = {
    "Blaze": {
        "description": "A balanced street brawler with a fiery temper.",
        "health": 100,       # hit points
        "speed": 5.0,        # walking speed (pixels per frame)
        "jump": 18,          # jump strength
        "punch": 7,          # punch damage
        "kick": 10,          # kick damage
        "defense": 1.0,      # damage taken is multiplied by this (lower = tougher)
        "gi": (200, 45, 45),
        "trim": (250, 205, 60),
        "skin": (238, 195, 150),
        "hair": (70, 40, 20),
    },
    "Titan": {
        "description": "A mountain of muscle. Slow, but hits like a truck.",
        "health": 130,
        "speed": 3.6,
        "jump": 16,
        "punch": 10,
        "kick": 14,
        "defense": 0.85,
        "gi": (95, 95, 110),
        "trim": (210, 70, 40),
        "skin": (175, 125, 90),
        "hair": (25, 25, 25),
    },
    "Viper": {
        "description": "Lightning fast and hard to pin down, but fragile.",
        "health": 85,
        "speed": 6.8,
        "jump": 20,
        "punch": 6,
        "kick": 8,
        "defense": 1.1,
        "gi": (40, 160, 80),
        "trim": (25, 25, 25),
        "skin": (225, 185, 140),
        "hair": (235, 215, 90),
    },
    "Frost": {
        "description": "A calm martial artist from the frozen north.",
        "health": 100,
        "speed": 5.0,
        "jump": 18,
        "punch": 7,
        "kick": 9,
        "defense": 0.95,
        "gi": (70, 140, 225),
        "trim": (230, 240, 255),
        "skin": (245, 215, 190),
        "hair": (235, 235, 245),
    },
    "Shade": {
        "description": "A ninja who strikes from the shadows.",
        "health": 90,
        "speed": 6.2,
        "jump": 19.5,
        "punch": 7,
        "kick": 9,
        "defense": 1.05,
        "gi": (60, 45, 95),
        "trim": (180, 70, 230),
        "skin": (205, 165, 125),
        "hair": (20, 20, 30),
    },
    "Rocco": {
        "description": "A heavyweight boxer. Big fists, weak kicks.",
        "health": 115,
        "speed": 4.5,
        "jump": 17,
        "punch": 10,
        "kick": 7,
        "defense": 0.95,
        "gi": (235, 145, 35),
        "trim": (40, 40, 40),
        "skin": (150, 100, 70),
        "hair": (30, 20, 10),
    },
}

POWERS = {
    "Fireball": {
        "description": "Throw a ball of fire straight at your opponent.",
        "type": "projectile",
        "damage": 12,
        "speed": 10,         # pixels per frame
        "cost": 30,          # energy needed
        "cooldown": 1.2,     # seconds before it can be used again
        "color": (255, 130, 30),
    },
    "Ice Shard": {
        "description": "A fast shard of ice that slows the enemy down.",
        "type": "projectile",
        "damage": 8,
        "speed": 14,
        "freeze": 2.0,       # seconds the target moves at half speed
        "cost": 25,
        "cooldown": 1.5,
        "color": (140, 220, 255),
    },
    "Thunder Strike": {
        "description": "Call lightning down where your opponent stands. It can't be blocked!",
        "type": "strike",
        "damage": 15,
        "delay": 0.7,        # seconds between the warning and the bolt
        "cost": 45,
        "cooldown": 4.0,
        "color": (255, 240, 90),
    },
    "Dragon Uppercut": {
        "description": "A rising uppercut that launches the enemy into the air.",
        "type": "uppercut",
        "damage": 14,
        "cost": 25,
        "cooldown": 2.0,
        "color": (255, 90, 60),
    },
    "Shadow Dash": {
        "description": "Dash across the floor and strike anything in your path.",
        "type": "dash",
        "damage": 10,
        "cost": 20,
        "cooldown": 1.5,
        "color": (170, 80, 255),
    },
    "Healing Light": {
        "description": "Restore some of your lost health.",
        "type": "heal",
        "amount": 20,
        "cost": 50,
        "cooldown": 8.0,
        "color": (110, 255, 140),
    },
    "Iron Shield": {
        "description": "A barrier that blocks all damage for a short time.",
        "type": "shield",
        "duration": 2.5,
        "cost": 35,
        "cooldown": 7.0,
        "color": (120, 180, 255),
    },
    "Berserk Rage": {
        "description": "Your attacks deal 50% more damage for a while.",
        "type": "rage",
        "multiplier": 1.5,
        "duration": 5.0,
        "cost": 40,
        "cooldown": 10.0,
        "color": (255, 60, 60),
    },
}

# Frame data for every attacking move (60 frames = 1 second).
#   startup / active / recovery - how long each part of the move lasts
#   reach    - how far in front of the fighter the hit box goes (before FIGHTER_SCALE)
#   height   - where the hit box is, as a fraction of body height
#   size     - how tall the hit box is (before FIGHTER_SCALE)
ATTACKS = {
    "punch": {"startup": 4, "active": 4, "recovery": 9, "reach": 62, "height": 0.75,
              "air_height": 0.62, "size": 28, "hitstun": 16, "knockback": 4.0, "sound": "punch"},
    "kick": {"startup": 7, "active": 5, "recovery": 13, "reach": 80, "height": 0.45,
             "air_height": 0.15, "size": 34, "hitstun": 20, "knockback": 6.5, "sound": "kick"},
    "uppercut": {"startup": 3, "active": 14, "recovery": 12, "reach": 58, "height": 0.8,
                 "size": 90, "hitstun": 34, "knockback": 3.0, "launch": -13, "sound": "kick"},
    "dash": {"startup": 3, "active": 14, "recovery": 10, "reach": 45, "height": 0.6,
             "size": 100, "hitstun": 24, "knockback": 9.0, "sound": "kick"},
}

# Each action maps to a list of keys, so a player can have more than one key per action.
CONTROLS = {
    1: {
        "left": [pygame.K_a],
        "right": [pygame.K_d],
        "up": [pygame.K_w],
        "down": [pygame.K_s],
        "punch": [pygame.K_f],
        "kick": [pygame.K_g],
        "power1": [pygame.K_r],
        "power2": [pygame.K_t],
    },
    2: {
        "left": [pygame.K_LEFT],
        "right": [pygame.K_RIGHT],
        "up": [pygame.K_UP],
        "down": [pygame.K_DOWN],
        "punch": [pygame.K_k, pygame.K_KP1],
        "kick": [pygame.K_l, pygame.K_KP2],
        "power1": [pygame.K_i, pygame.K_KP4],
        "power2": [pygame.K_o, pygame.K_KP5],
    },
}

CONTROL_HINTS = {
    "Move": ("A / D", "LEFT / RIGHT"),
    "Jump": ("W", "UP"),
    "Block (crouch)": ("S", "DOWN"),
    "Punch": ("F", "K"),
    "Kick": ("G", "L"),
    "Power 1": ("R", "I"),
    "Power 2": ("T", "O"),
}


def key_label(player, action):
    """The first key bound to an action, e.g. key_label(1, "power1") -> "R"."""
    return pygame.key.name(CONTROLS[player][action][0]).upper()
