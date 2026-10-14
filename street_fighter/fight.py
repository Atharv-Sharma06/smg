"""The fight: rounds, timer, collisions, the HUD and the winner screen."""
import math
import random

import pygame

from art import blend
from effects import FloatingText, burst
from fighter import POWER_SLOTS, Fighter
from game_data import ATTACKS, key_label
from settings import (BLACK, FPS, GREY, GROUND_Y, HEIGHT, MAX_ENERGY, MAX_ROUNDS, PLAYER_COLORS, RED,
                      ROUND_TIME, ROUNDS_TO_WIN, STAGE_LEFT, STAGE_RIGHT, WHITE, WIDTH, YELLOW)
from ui import draw_panel, draw_text, outlined

START_KEYS = (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE)
PAUSE_KEYS = (pygame.K_ESCAPE, pygame.K_p)
STATUS_TAGS = {"freeze": ("FROZEN", (150, 220, 255)), "shield": ("SHIELD", (120, 180, 255)),
               "rage": ("RAGE", (255, 80, 80))}


class FightScreen:
    INTRO_TIME = 140          # "ROUND 1 ... FIGHT!"
    ROUND_OVER_TIME = 200     # "K.O.!" and who won the round

    def __init__(self, game, choices):
        self.game = game
        self.fonts = game.fonts
        self.choices = choices
        self.wins = {1: 0, 2: 0}
        self.round_number = 1
        self.champion = None
        self.paused = False
        self.world = pygame.Surface((WIDTH, HEIGHT))
        self.dim = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        self.dim.fill((0, 0, 0, 150))
        self.light_dim = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        self.light_dim.fill((0, 0, 0, 80))
        self.start_round()

    def start_round(self):
        p1, p2 = self.choices[1], self.choices[2]
        self.fighters = {
            1: Fighter(1, p1["character"], p1["powers"], WIDTH * 0.3, facing=1),
            2: Fighter(2, p2["character"], p2["powers"], WIDTH * 0.7, facing=-1,
                       alt_colors=p1["character"] == p2["character"]),
        }
        self.projectiles = []
        self.strikes = []
        self.particles = []
        self.texts = []
        self.time_left = ROUND_TIME * FPS
        self.phase = "intro"
        self.phase_timer = 0
        self.combat_active = False    # attacks only connect while this is True
        self.freeze_frames = 0        # "hit stop": the action pauses briefly on a hit
        self.shake_power = 0.0
        self.banner = ""
        self.round_winner = None
        self.perfect = False
        self.play("round")

    # ------------------------------------------------------------------ used by the fighters
    def play(self, name):
        self.game.sounds.play(name)

    def burst(self, x, y, color, count=12, speed=5.0, gravity=0.15, size=5):
        burst(self.particles, x, y, color, count, speed, gravity, size)

    def float_text(self, text, x, y, color):
        self.texts.append(FloatingText(text, x, y, color, self.fonts["small"]))

    def hitstop(self, frames):
        self.freeze_frames = max(self.freeze_frames, frames)

    def shake(self, amount):
        self.shake_power = max(self.shake_power, amount)

    def opponent_of(self, fighter):
        return self.fighters[2 if fighter.number == 1 else 1]

    # ------------------------------------------------------------------ input
    def handle_event(self, event):
        if event.type != pygame.KEYDOWN:
            return
        key = event.key
        if self.paused:
            if key in PAUSE_KEYS:
                self.paused = False
            elif key == pygame.K_q:
                self.game.start_select()
            return
        if self.phase == "match_over":
            if key in START_KEYS:
                self.game.start_fight(self.choices)
            elif key == pygame.K_ESCAPE:
                self.game.start_select()
            return
        if key in PAUSE_KEYS:
            self.paused = True
            return
        if self.phase == "fight":
            for fighter in self.fighters.values():
                fighter.press(key)

    # ------------------------------------------------------------------ update
    def update(self):
        if self.paused:
            return
        for item in self.particles + self.texts:
            item.update()
        self.particles = [p for p in self.particles if p.alive]
        self.texts = [t for t in self.texts if t.alive]
        self.shake_power = self.shake_power * 0.88 if self.shake_power > 0.3 else 0.0

        if self.freeze_frames > 0:
            self.freeze_frames -= 1
            return

        self.phase_timer += 1
        keys = pygame.key.get_pressed()

        if self.phase == "intro":
            self._step(keys, controllable=False)
            if self.phase_timer == 90:
                self.play("fight")
            if self.phase_timer >= self.INTRO_TIME:
                self.phase = "fight"
                self.phase_timer = 0
                self.combat_active = True
        elif self.phase == "fight":
            self.time_left -= 1
            self._step(keys, controllable=True)
            self._check_round_over()
        elif self.phase == "round_over":
            if self.phase_timer < 60 and self.phase_timer % 2:
                return  # slow motion right after the knockout
            self._step(keys, controllable=False)
            winner = self.fighters.get(self.round_winner)
            if (winner and self.phase_timer >= 100 and winner.on_ground
                    and winner.state not in ("victory", "ko")):
                winner.attack = None
                winner.set_state("victory")
            if self.phase_timer >= self.ROUND_OVER_TIME:
                self._next_round()
        elif self.phase == "match_over":
            self._step(keys, controllable=False)

    def _step(self, keys, controllable):
        p1, p2 = self.fighters[1], self.fighters[2]
        p1.update(keys, p2, self, controllable)
        p2.update(keys, p1, self, controllable)
        self._separate(p1, p2)
        self._update_projectiles()
        self._update_strikes()

    @staticmethod
    def _separate(a, b):
        """Stop the fighters from walking through each other."""
        if a.state == "ko" or b.state == "ko" or abs(a.y - b.y) > 120:
            return
        gap = b.x - a.x
        if abs(gap) >= Fighter.WIDTH:
            return
        side = 1 if gap > 0 else -1 if gap < 0 else a.facing
        push = (Fighter.WIDTH - abs(gap)) / 2
        a.x = max(STAGE_LEFT, min(STAGE_RIGHT, a.x - push * side))
        b.x = max(STAGE_LEFT, min(STAGE_RIGHT, b.x + push * side))
        if abs(b.x - a.x) < Fighter.WIDTH:   # one of them is against the wall
            if a.x in (STAGE_LEFT, STAGE_RIGHT):
                b.x = a.x + Fighter.WIDTH * side
            else:
                a.x = b.x - Fighter.WIDTH * side

    def _update_projectiles(self):
        for projectile in self.projectiles:
            projectile.update(self.particles)

        # two projectiles that meet cancel each other out
        for i, a in enumerate(self.projectiles):
            for b in self.projectiles[i + 1:]:
                if a.alive and b.alive and a.owner is not b.owner and a.rect().colliderect(b.rect()):
                    a.alive = b.alive = False
                    self.burst((a.x + b.x) / 2, a.y, WHITE, 20, speed=6)
                    self.play("block")

        for projectile in self.projectiles:
            if not projectile.alive or not self.combat_active:
                continue
            target = self.opponent_of(projectile.owner)
            if projectile.rect().colliderect(target.hurtbox()):
                result = target.receive_hit(projectile.damage, projectile.x - projectile.vx * 4, 18, 5.0, self,
                                            freeze=projectile.info.get("freeze", 0), sound="kick")
                if result != "miss":
                    projectile.alive = False
                    self.burst(projectile.x, projectile.y, projectile.color, 18, speed=6)
                if result in ("hit", "ko"):
                    projectile.owner.gain_energy(4)
        self.projectiles = [p for p in self.projectiles if p.alive]

    def _update_strikes(self):
        for strike in self.strikes:
            strike.update()
            if not strike.striking_now:
                continue
            self.play("zap")
            self.shake(8)
            self.burst(strike.x, GROUND_Y - 5, strike.info["color"], 24, speed=7, gravity=0.25)
            target = self.opponent_of(strike.owner)
            if self.combat_active and strike.hit_rect().colliderect(target.hurtbox()):
                result = target.receive_hit(strike.damage, strike.x, 28, 2.0, self, unblockable=True, sound=None)
                if result in ("hit", "ko"):
                    strike.owner.gain_energy(4)
        self.strikes = [s for s in self.strikes if s.alive]

    # ------------------------------------------------------------------ rounds
    def _check_round_over(self):
        p1, p2 = self.fighters[1], self.fighters[2]
        down1, down2 = p1.state == "ko", p2.state == "ko"
        if down1 and down2:
            self._end_round(None, "DOUBLE K.O.")
        elif down1 or down2:
            self._end_round(2 if down1 else 1, "K.O.!")
        elif self.time_left <= 0:
            share1, share2 = p1.health / p1.max_health, p2.health / p2.max_health
            winner = 1 if share1 > share2 else 2 if share2 > share1 else None
            self._end_round(winner, "TIME!")

    def _end_round(self, winner, banner):
        self.phase = "round_over"
        self.phase_timer = 0
        self.combat_active = False
        self.banner = banner
        self.round_winner = winner
        if winner:
            self.wins[winner] += 1
            fighter = self.fighters[winner]
            self.perfect = fighter.health >= fighter.max_health
        if banner == "TIME!":
            self.play("round")

    def _next_round(self):
        champion = next((n for n, wins in self.wins.items() if wins >= ROUNDS_TO_WIN), None)
        if champion or self.round_number >= MAX_ROUNDS:
            self.phase = "match_over"
            self.phase_timer = 0
            self.champion = champion
            if champion and self.fighters[champion].state != "ko":
                self.fighters[champion].set_state("victory")
            self.play("select")
        else:
            self.round_number += 1
            self.start_round()

    # ------------------------------------------------------------------ drawing
    def draw(self, screen):
        world = self.world
        world.blit(self.game.stage, (0, 0))
        for strike in self.strikes:
            strike.draw_warning(world)
        # whoever is attacking is drawn in front
        for fighter in sorted(self.fighters.values(), key=lambda f: f.state in ATTACKS or f.state == "cast"):
            fighter.draw(world)
        for projectile in self.projectiles:
            projectile.draw(world)
        for strike in self.strikes:
            strike.draw(world)
        for particle in self.particles:
            particle.draw(world)
        if self.phase in ("intro", "fight"):
            self._draw_markers(world)
        for text in self.texts:
            text.draw(world)

        offset = (0, 0)
        if self.shake_power:
            offset = (random.uniform(-1, 1) * self.shake_power, random.uniform(-1, 1) * self.shake_power)
        screen.fill(BLACK)
        screen.blit(world, offset)

        self._draw_hud(screen)
        self._draw_banners(screen)
        if self.paused:
            self._draw_pause(screen)

    def _draw_markers(self, surface):
        """A small "P1" / "P2" arrow above each fighter."""
        for number, fighter in self.fighters.items():
            x, y = fighter.x, fighter.hurtbox().top - 22
            color = PLAYER_COLORS[number]
            pygame.draw.polygon(surface, color, [(x - 6, y), (x + 6, y), (x, y + 8)])
            draw_text(surface, f"P{number}", self.fonts["tiny"], color, (x, y - 10), outline=2)

    def _draw_hud(self, screen):
        for number, fighter in self.fighters.items():
            self._draw_health(screen, number, fighter)
            self._draw_power_panel(screen, number, fighter)

        # timer
        box = pygame.Rect(WIDTH // 2 - 42, 12, 84, 60)
        draw_panel(screen, box, alpha=200, border=WHITE)
        seconds = max(0, math.ceil(self.time_left / FPS))
        draw_text(screen, str(seconds), self.fonts["big"], RED if seconds <= 10 else YELLOW, box.center)
        draw_text(screen, f"ROUND {self.round_number}", self.fonts["tiny"], WHITE, (WIDTH // 2, box.bottom + 12))

    def _draw_health(self, screen, number, fighter):
        left = number == 1
        bar = pygame.Rect(30, 26, 380, 26) if left else pygame.Rect(WIDTH - 410, 26, 380, 26)
        pygame.draw.rect(screen, (70, 15, 20), bar)

        def part(fraction):
            width = int(bar.width * max(0.0, fraction))
            return pygame.Rect(bar.x if left else bar.right - width, bar.y, width, bar.height)

        pygame.draw.rect(screen, (255, 110, 50), part(fighter.shown_health / fighter.max_health))
        fraction = fighter.health / fighter.max_health
        color = YELLOW if fraction > 0.3 else blend(RED, YELLOW, (math.sin(self.phase_timer * 0.3) + 1) / 4)
        fill = part(fraction)
        pygame.draw.rect(screen, color, fill)
        pygame.draw.rect(screen, blend(color, WHITE, 0.5), (fill.x, fill.y + 4, fill.width, 6))
        pygame.draw.rect(screen, WHITE, bar, 2)

        # name and status tags under the bar
        label = f"P{number}  {fighter.character.upper()}"
        tags = [(text, tag_color) for effect, (text, tag_color) in STATUS_TAGS.items() if fighter.effects[effect]]
        anchor = "topleft" if left else "topright"
        rect = draw_text(screen, label, self.fonts["small"], PLAYER_COLORS[number],
                         (bar.x if left else bar.right, bar.bottom + 5), anchor=anchor)
        x = rect.right + 10 if left else rect.left - 10
        for text, tag_color in tags:
            tag = draw_text(screen, text, self.fonts["tiny"], tag_color, (x, rect.centery),
                            anchor="midleft" if left else "midright")
            x = tag.right + 8 if left else tag.left - 8

        # round wins
        for i in range(ROUNDS_TO_WIN):
            cx = bar.right - 12 - i * 26 if left else bar.x + 12 + i * 26
            won = i < self.wins[number]
            pygame.draw.circle(screen, YELLOW if won else (50, 50, 60), (cx, bar.bottom + 16), 8)
            pygame.draw.circle(screen, WHITE, (cx, bar.bottom + 16), 8, 2)

    def _draw_power_panel(self, screen, number, fighter):
        left = number == 1
        x = 30 if left else WIDTH - 430
        draw_panel(screen, (x - 8, 534, 416, 60), alpha=160, radius=8)

        bar = pygame.Rect(x, 541, 400, 10)
        pygame.draw.rect(screen, (20, 30, 60), bar)
        width = int(bar.width * fighter.energy / MAX_ENERGY)
        fill = pygame.Rect(bar.x if left else bar.right - width, bar.y, width, bar.height)
        pygame.draw.rect(screen, (70, 190, 255), fill)
        pygame.draw.rect(screen, (200, 230, 255), bar, 1)

        for i, slot in enumerate(POWER_SLOTS):
            power = fighter.powers.get(slot)
            if not power:
                continue
            info = power["info"]
            rect = pygame.Rect(x + i * 205, 557, 195, 30)
            affordable = fighter.energy >= info["cost"]
            ready = affordable and power["cooldown"] == 0
            pygame.draw.rect(screen, blend((25, 25, 35), info["color"], 0.35 if ready else 0.1), rect,
                             border_radius=6)
            if power["cooldown"]:
                total = info["cooldown"] * FPS
                shade_width = int(rect.width * power["cooldown"] / total)
                pygame.draw.rect(screen, (10, 10, 15), (rect.x, rect.y, shade_width, rect.height),
                                 border_radius=6)
            pulse = (math.sin(self.phase_timer * 0.15) + 1) / 2
            border = blend(info["color"], WHITE, pulse * 0.5) if ready else (70, 70, 85)
            pygame.draw.rect(screen, border, rect, 2, border_radius=6)

            badge = pygame.Rect(rect.x + 4, rect.y + 4, 22, 22)
            pygame.draw.rect(screen, WHITE if ready else GREY, badge, border_radius=4)
            draw_text(screen, key_label(number, slot), self.fonts["tiny"], (20, 20, 30), badge.center, outline=0)
            draw_text(screen, power["name"], self.fonts["tiny"], WHITE if ready else GREY,
                      (rect.x + 32, rect.centery), anchor="midleft", outline=1)
            draw_text(screen, str(info["cost"]), self.fonts["tiny"], (120, 200, 255) if affordable else RED,
                      (rect.right - 8, rect.centery), anchor="midright", outline=1)

    def _banner(self, screen, text, color, y=250, zoom=1.0, font="huge"):
        image = outlined(self.fonts[font], text, color, 4)
        if zoom != 1.0:
            image = pygame.transform.rotozoom(image, 0, zoom)
        screen.blit(image, image.get_rect(center=(WIDTH // 2, y)))

    @staticmethod
    def _pop(t):
        """Zoom that starts big and settles to normal size - makes banners 'slam' in."""
        return 1.0 + max(0.0, (12 - t) / 12) * 1.5

    def _draw_banners(self, screen):
        t = self.phase_timer
        if self.phase == "intro":
            if t < 90:
                final = all(wins == ROUNDS_TO_WIN - 1 for wins in self.wins.values())
                text = "FINAL ROUND" if final else f"ROUND {self.round_number}"
                self._banner(screen, text, YELLOW, zoom=self._pop(t))
            else:
                self._banner(screen, "FIGHT!", RED, zoom=self._pop(t - 90))
        elif self.phase == "round_over":
            if t < 100:
                self._banner(screen, self.banner, RED, zoom=self._pop(t))
            elif self.round_winner:
                fighter = self.fighters[self.round_winner]
                self._banner(screen, f"{fighter.character.upper()} WINS", PLAYER_COLORS[self.round_winner],
                             zoom=self._pop(t - 100), font="big")
                if self.perfect:
                    self._banner(screen, "PERFECT!", YELLOW, y=320, font="medium")
            else:
                self._banner(screen, "DRAW", WHITE, zoom=self._pop(t - 100), font="big")
        elif self.phase == "match_over":
            screen.blit(self.light_dim, (0, 0))
            if self.champion:
                fighter = self.fighters[self.champion]
                self._banner(screen, f"PLAYER {self.champion} WINS!", PLAYER_COLORS[self.champion], y=170,
                             zoom=self._pop(t), font="big")
                self._banner(screen, f"{fighter.character} is the champion!", WHITE, y=235, font="medium")
            else:
                self._banner(screen, "DRAW GAME", WHITE, y=170, zoom=self._pop(t), font="big")
            self._banner(screen, f"{self.wins[1]}  -  {self.wins[2]}", YELLOW, y=290, font="medium")
            if (t // 30) % 2 == 0:
                self._banner(screen, "ENTER  rematch        ESC  change fighters", WHITE, y=345, font="small")

    def _draw_pause(self, screen):
        screen.blit(self.dim, (0, 0))
        draw_panel(screen, (WIDTH // 2 - 300, 160, 600, 190), alpha=200, border=YELLOW)
        self._banner(screen, "PAUSED", YELLOW, y=230, font="huge")
        self._banner(screen, "P / ESC  resume        Q  back to character select", WHITE, y=310, font="small")
