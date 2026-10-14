"""The Fighter class: movement, attacks, powers, taking hits and drawing."""
import math

import pygame

from art import POSES, alternate_costume, draw_fighter, draw_glow, idle_pose, lerp_pose, victory_pose, walk_pose
from effects import LightningStrike, Projectile
from game_data import ATTACKS, CHARACTERS, CONTROLS, POWERS
from settings import (ENERGY_REGEN, FIGHTER_SCALE, FPS, GRAVITY, GROUND_Y, MAX_ENERGY, STAGE_LEFT,
                      STAGE_RIGHT, START_ENERGY, YELLOW)

FREE_STATES = ("idle", "walk", "crouch", "jump")   # states where the player is in control
POWER_SLOTS = ("power1", "power2")
CAST_TIME = 26       # length of the power-casting animation (frames)
CAST_RELEASE = 9     # frame of that animation where the power comes out
INPUT_BUFFER = 8     # a button press is remembered for this many frames


class Fighter:
    # Body size in pixels (the drawing is about 130 x 50 before scaling)
    WIDTH = int(48 * FIGHTER_SCALE)
    HEIGHT = int(130 * FIGHTER_SCALE)
    CROUCH_HEIGHT = int(88 * FIGHTER_SCALE)

    def __init__(self, number, character, power_names, x, facing, alt_colors=False):
        self.number = number
        self.character = character
        self.stats = CHARACTERS[character]
        self.controls = CONTROLS[number]

        # Colours used for drawing. If both players pick the same fighter,
        # player 2 gets a lighter "alternate costume".
        self.look = alternate_costume(self.stats) if alt_colors else self.stats

        # The player's chosen powers, keyed by the button that fires them.
        # Each one keeps track of its own cooldown.
        self.powers = {
            slot: {"name": name, "info": POWERS[name], "cooldown": 0}
            for slot, name in zip(POWER_SLOTS, power_names)
        }

        # Timed status effects, counted down in frames
        self.effects = {"freeze": 0, "shield": 0, "rage": 0}
        self.rage_multiplier = 1.0

        self.x, self.y = float(x), float(GROUND_Y)
        self.vx = self.vy = 0.0
        self.facing = facing
        self.on_ground = True
        self.air_attacked = False

        self.max_health = self.stats["health"]
        self.health = float(self.max_health)
        self.shown_health = float(self.max_health)   # lags behind health in the HUD
        self.energy = float(START_ENERGY)

        self.state = "idle"
        self.state_timer = 0
        self.stun = 0
        self.attack = None        # frame data of the move being performed
        self.attack_hit = False
        self.casting = None       # power being cast
        self.buffer = {}          # recently pressed buttons: action -> frames left
        self.flash = 0
        self.walk_cycle = 0.0
        self.anim = 0
        self.trail = []           # after-images left by Shadow Dash

    # ------------------------------------------------------------------ input
    def press(self, key):
        """Remember a key press for a few frames so moves feel responsive."""
        for action, keys in self.controls.items():
            if key in keys:
                self.buffer[action] = INPUT_BUFFER

    def pressed(self, action):
        """True (once) if the action's button was pressed recently."""
        if self.buffer.get(action, 0) > 0:
            del self.buffer[action]
            return True
        return False

    def holding(self, keys, action):
        return any(keys[k] for k in self.controls[action])

    # ------------------------------------------------------------------ helpers
    @property
    def speed(self):
        return self.stats["speed"] * (0.5 if self.effects["freeze"] else 1.0)

    @property
    def damage_bonus(self):
        return self.rage_multiplier if self.effects["rage"] else 1.0

    @property
    def is_blocking(self):
        return self.on_ground and self.state in ("crouch", "blockstun")

    @property
    def text_y(self):
        """Where floating text above this fighter's head starts."""
        return self.y - self.HEIGHT - 55

    def gain_energy(self, amount):
        self.energy = min(MAX_ENERGY, self.energy + amount)

    def set_state(self, state):
        self.state = state
        self.state_timer = 0

    def hurtbox(self):
        if self.state == "ko" and self.on_ground:
            return pygame.Rect(int(self.x - self.HEIGHT / 2), int(self.y - 36), self.HEIGHT, 36)
        height = self.CROUCH_HEIGHT if self.is_blocking else self.HEIGHT
        return pygame.Rect(int(self.x - self.WIDTH / 2), int(self.y - height), self.WIDTH, height)

    def hitbox(self):
        attack = self.attack
        height = attack["air_height"] if not self.on_ground and "air_height" in attack else attack["height"]
        center_y = self.y - self.HEIGHT * height
        reach, size = attack["reach"] * FIGHTER_SCALE, attack["size"] * FIGHTER_SCALE
        left = self.x + 12 if self.facing == 1 else self.x - reach
        return pygame.Rect(int(left), int(center_y - size / 2), int(reach - 12), int(size))

    # ------------------------------------------------------------------ update
    def update(self, keys, opponent, world, controllable):
        self._tick_timers(controllable)
        self.state_timer += 1

        if self.state in FREE_STATES and self.on_ground and opponent.x != self.x:
            self.facing = 1 if opponent.x > self.x else -1

        if self.state == "hit":
            self.stun -= 1
            if self.on_ground:
                self.vx *= 0.85
                if self.stun <= 0:
                    self.set_state("idle")
        elif self.state == "blockstun":
            self.vx *= 0.8
            self.stun -= 1
            if self.stun <= 0:
                self.set_state("crouch")
        elif self.state in ATTACKS:
            self._update_attack(opponent, world)
        elif self.state == "cast":
            if self.state_timer == CAST_RELEASE:
                self._release_power(opponent, world)
            if self.state_timer >= CAST_TIME:
                self.set_state("idle")
        elif self.state in FREE_STATES:
            if controllable:
                self._handle_input(keys, world)
            elif self.on_ground:
                self.vx = 0
                if self.state != "idle":
                    self.set_state("idle")
        elif self.on_ground:      # knocked out or celebrating
            self.vx *= 0.85

        if self.state == "walk":
            self.walk_cycle += 0.035 * self.vx * self.facing
        self._apply_physics(world)
        self._update_trail()

    def _tick_timers(self, controllable):
        self.anim += 1
        for name in self.effects:
            if self.effects[name] > 0:
                self.effects[name] -= 1
        for power in self.powers.values():
            if power["cooldown"] > 0:
                power["cooldown"] -= 1
        for action in list(self.buffer):
            self.buffer[action] -= 1
            if self.buffer[action] <= 0:
                del self.buffer[action]
        if self.flash > 0:
            self.flash -= 1
        if controllable:
            self.gain_energy(ENERGY_REGEN)
        gap = self.shown_health - self.health
        if gap > 0:
            self.shown_health -= min(gap, max(0.4, gap * 0.04))
        else:
            self.shown_health = self.health

    def _handle_input(self, keys, world):
        # Powers can only be used from the ground. If pressed in the air the
        # press stays buffered and fires on landing.
        if self.on_ground:
            for slot in POWER_SLOTS:
                if slot in self.powers and self.pressed(slot) and self.use_power(slot, world):
                    return

        if self.on_ground or not self.air_attacked:
            for move in ("punch", "kick"):
                if self.pressed(move):
                    self.start_attack(move, self.stats[move], world)
                    return

        if not self.on_ground:
            return

        direction = int(self.holding(keys, "right")) - int(self.holding(keys, "left"))
        if self.holding(keys, "up"):
            self.vy = -self.stats["jump"]
            self.vx = direction * self.speed
            self.on_ground = False
            self.set_state("jump")
        elif self.holding(keys, "down"):
            self.vx = 0
            if self.state != "crouch":
                self.set_state("crouch")
        else:
            self.vx = direction * self.speed
            new_state = "walk" if direction else "idle"
            if self.state != new_state:
                self.set_state(new_state)

    # ------------------------------------------------------------------ attacks
    def start_attack(self, name, damage, world):
        self.attack = dict(ATTACKS[name], name=name, damage=damage * self.damage_bonus)
        self.attack_hit = False
        self.set_state(name)
        if name == "uppercut":
            self.vy = -15
            self.vx = self.facing * 2.5
            self.on_ground = False
        elif self.on_ground:
            self.vx = 0
        else:
            self.air_attacked = True
        world.play("swing")

    def _update_attack(self, opponent, world):
        attack = self.attack
        t = self.state_timer
        active_start = attack["startup"]
        active_end = active_start + attack["active"]
        total = active_end + attack["recovery"]

        if self.state == "dash":
            self.vx = self.facing * 16 if t < active_end else self.vx * 0.75

        if (world.combat_active and active_start <= t < active_end and not self.attack_hit
                and self.hitbox().colliderect(opponent.hurtbox())):
            self.attack_hit = True
            result = opponent.receive_hit(attack["damage"], self.x, attack["hitstun"], attack["knockback"],
                                          world, launch=attack.get("launch", 0), sound=attack["sound"])
            if result in ("hit", "ko"):
                self.gain_energy(6)

        if t >= total and (self.on_ground or self.state in ("punch", "kick")):
            self.attack = None
            self.set_state("idle" if self.on_ground else "jump")

    # ------------------------------------------------------------------ powers
    def use_power(self, slot, world):
        """Try to fire the power in a slot. Returns True if it was used."""
        power = self.powers[slot]
        info = power["info"]
        if power["cooldown"] > 0:
            world.float_text("COOLDOWN", self.x, self.text_y, (190, 190, 200))
            world.play("deny")
            return False
        if self.energy < info["cost"]:
            world.float_text("NO ENERGY", self.x, self.text_y, (190, 190, 200))
            world.play("deny")
            return False

        self.energy -= info["cost"]
        power["cooldown"] = int(info["cooldown"] * FPS)
        world.float_text(power["name"].upper(), self.x, self.text_y, info["color"])
        world.burst(self.x, self.y - self.HEIGHT / 2, info["color"], 14, speed=4)
        world.play("power")

        if info["type"] in ("uppercut", "dash"):
            self.start_attack(info["type"], info["damage"], world)
        else:
            self.casting = info
            self.vx = 0
            self.set_state("cast")
        return True

    def _release_power(self, opponent, world):
        info = self.casting
        kind = info["type"]
        if kind == "projectile":
            hand_x = self.x + self.facing * 55 * FIGHTER_SCALE
            hand_y = self.y - 93 * FIGHTER_SCALE
            world.projectiles.append(Projectile(self, info, hand_x, hand_y, self.facing,
                                                info["damage"] * self.damage_bonus))
            world.play("fire")
        elif kind == "strike":
            world.strikes.append(LightningStrike(self, info, opponent.x, info["damage"] * self.damage_bonus))
        elif kind == "heal":
            healed = min(info["amount"], self.max_health - self.health)
            self.health += healed
            world.float_text(f"+{round(healed)}", self.x, self.text_y + 25, info["color"])
            world.burst(self.x, self.y - self.HEIGHT / 2, info["color"], 24, speed=3, gravity=-0.12)
            world.play("heal")
        elif kind == "shield":
            self.effects["shield"] = int(info["duration"] * FPS)
        elif kind == "rage":
            self.effects["rage"] = int(info["duration"] * FPS)
            self.rage_multiplier = info["multiplier"]
        self.casting = None

    # ------------------------------------------------------------------ getting hit
    def receive_hit(self, damage, from_x, hitstun, knockback, world,
                    launch=0, freeze=0, unblockable=False, sound="punch"):
        """Called when an attack connects. Returns "hit", "block", "shield", "ko" or "miss"."""
        if self.state == "ko":
            return "miss"
        away = 1 if self.x >= from_x else -1
        spark_x = self.x - away * self.WIDTH * 0.4
        spark_y = self.y - (self.CROUCH_HEIGHT * 0.7 if self.is_blocking else self.HEIGHT * 0.62)

        if self.effects["shield"]:
            self.vx = away * 2
            world.burst(spark_x, spark_y, (150, 210, 255), 10)
            world.play("block")
            return "shield"

        if self.is_blocking and not unblockable:
            self.health = max(1.0, self.health - damage * 0.15)   # chip damage can't knock out
            self.gain_energy(3)
            self.set_state("blockstun")
            self.stun = 8 + hitstun // 3
            self.vx = away * knockback * 0.9
            world.burst(spark_x, spark_y, (200, 230, 255), 8, speed=4)
            world.play("block")
            world.hitstop(3)
            return "block"

        damage *= self.stats["defense"]
        self.health -= damage
        self.gain_energy(5)
        self.flash = 10
        self.attack = None
        self.casting = None
        world.burst(spark_x, spark_y, (255, 210, 90), 16, speed=7)
        world.float_text(str(round(damage)), spark_x, spark_y - 30, YELLOW)
        if sound:
            world.play(sound)
        world.hitstop(4 + int(damage // 4))
        world.shake(damage * 0.6)
        if freeze:
            self.effects["freeze"] = int(freeze * FPS)
            world.float_text("FROZEN!", self.x, self.text_y - 25, (150, 220, 255))

        if self.health <= 0:
            self.health = 0
            self.set_state("ko")
            self.vx, self.vy = away * 7, -11
            self.on_ground = False
            world.play("ko")
            world.hitstop(25)
            world.shake(14)
            return "ko"

        self.set_state("hit")
        self.stun = hitstun
        self.vx = away * knockback
        if launch:
            self.vy = launch
            self.on_ground = False
        elif not self.on_ground:
            self.vy = min(self.vy, -5)     # pop a fighter hit in the air up a little
        return "hit"

    # ------------------------------------------------------------------ physics
    def _apply_physics(self, world):
        if not self.on_ground:
            self.vy += GRAVITY
            self.y += self.vy
            if self.y >= GROUND_Y:
                self.y = GROUND_Y
                self.vy = 0
                self.on_ground = True
                self._land(world)
        self.x = max(STAGE_LEFT, min(STAGE_RIGHT, self.x + self.vx))

    def _land(self, world):
        self.air_attacked = False
        world.burst(self.x, GROUND_Y - 4, (150, 130, 130), 6, speed=2.5, gravity=0.05, size=4)
        if self.state in ("jump", "punch", "kick"):
            self.attack = None
            self.vx = 0
            self.set_state("idle")
        elif self.state == "uppercut":
            self.vx = 0
        elif self.state == "ko":
            self.vx *= 0.5
            world.shake(6)

    def _update_trail(self):
        for ghost in self.trail:
            ghost["age"] += 1
        self.trail = [ghost for ghost in self.trail if ghost["age"] < 12]
        if self.state == "dash" and self.anim % 2 == 0:
            self.trail.append({"x": self.x, "y": self.y, "facing": self.facing,
                               "pose": self.current_pose(), "age": 0})

    # ------------------------------------------------------------------ drawing
    def current_pose(self):
        state, t = self.state, self.state_timer
        if state == "walk":
            return walk_pose(self.walk_cycle)
        if state == "jump":
            return POSES["jump"]
        if state in ("crouch", "blockstun"):
            return POSES["crouch"]
        if state == "hit":
            return POSES["hit"]
        if state == "ko":
            return POSES["ko"] if self.on_ground else POSES["hit"]
        if state == "victory":
            return victory_pose(self.anim)
        if state == "cast":
            amount = min(1.0, t / 6, (CAST_TIME - t) / 8)
            return lerp_pose(idle_pose(self.anim), POSES["cast"], max(0.0, amount))
        if state in ATTACKS and self.attack:
            attack = self.attack
            in_air = not self.on_ground and state in ("punch", "kick")
            target = POSES["air_" + state if in_air else state]
            base = POSES["jump"] if in_air else idle_pose(self.anim)
            if t < attack["startup"]:
                amount = t / attack["startup"]
            elif t < attack["startup"] + attack["active"] or (state == "uppercut" and not self.on_ground):
                amount = 1.0
            else:
                amount = 1 - (t - attack["startup"] - attack["active"]) / attack["recovery"]
            return lerp_pose(base, target, max(0.0, amount))
        return idle_pose(self.anim)

    def draw(self, surface):
        # shadow on the floor, smaller the higher the fighter is
        width = max(30, 92 - (GROUND_Y - self.y) * 0.25)
        shadow = pygame.Surface((int(width), 14), pygame.SRCALPHA)
        pygame.draw.ellipse(shadow, (0, 0, 0, 90), shadow.get_rect())
        surface.blit(shadow, (self.x - width / 2, GROUND_Y - 7))

        for ghost in self.trail:
            draw_fighter(surface, ghost["x"], ghost["y"], ghost["facing"], self.look, ghost["pose"],
                         scale=FIGHTER_SCALE, tint=((110, 50, 190), min(0.95, 0.55 + ghost["age"] * 0.04)))

        center = (self.x, self.y - self.HEIGHT / 2)
        radius = self.HEIGHT * 0.65
        if self.effects["rage"]:
            draw_glow(surface, (255, 40, 40), center, radius * 0.92 + 6 * math.sin(self.anim * 0.3), 70)

        x = self.x + (2 if self.state == "blockstun" and self.anim % 4 < 2 else 0)
        tint = ((150, 220, 255), 0.45) if self.effects["freeze"] else None
        flash = self.flash > 0 and (self.flash // 2) % 2 == 0
        draw_fighter(surface, x, self.y, self.facing, self.look, self.current_pose(), scale=FIGHTER_SCALE,
                     flash=flash, tint=tint, ko=self.state == "ko")

        shield = self.effects["shield"]
        if shield and (shield > 40 or shield % 8 < 4):   # blinks when about to run out
            draw_glow(surface, (120, 180, 255), center, radius, 55)
            pygame.draw.circle(surface, (200, 230, 255), center, radius, 2)
