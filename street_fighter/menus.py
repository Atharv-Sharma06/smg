"""The title screen and the character / power select screen."""
import math

import pygame

from art import alternate_costume, blend, draw_fighter, draw_glow, idle_pose, victory_pose
from game_data import CHARACTERS, CONTROL_HINTS, CONTROLS, POWERS, key_label
from settings import GREY, HEIGHT, PLAYER_COLORS, WHITE, WIDTH, YELLOW
from ui import draw_panel, draw_text, wrap_text

START_KEYS = (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE)
CARD_COLOR = (30, 30, 46)

# How each stat bar on the select screen is worked out (result is 0..1)
STAT_BARS = {
    "Health": lambda c: (c["health"] - 70) / 70,
    "Speed": lambda c: (c["speed"] - 2.5) / 5,
    "Power": lambda c: (c["punch"] + c["kick"] - 8) / 20,
    "Defense": lambda c: (1.25 - c["defense"]) / 0.5,
}


def power_summary(info):
    """A one-line description of a power's numbers."""
    parts = [f"Energy {info['cost']}", f"Cooldown {info['cooldown']:g}s"]
    if "damage" in info:
        parts.append(f"Damage {info['damage']}")
    if "amount" in info:
        parts.append(f"Heals {info['amount']}")
    if "duration" in info:
        parts.append(f"Lasts {info['duration']:g}s")
    return "   ".join(parts)


class TitleScreen:
    def __init__(self, game):
        self.game = game
        self.t = 0
        self.overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        self.overlay.fill((10, 5, 25, 120))

    def handle_event(self, event):
        if event.type != pygame.KEYDOWN:
            return
        if event.key in START_KEYS:
            self.game.sounds.play("select")
            self.game.start_select()
        elif event.key == pygame.K_ESCAPE:
            self.game.quit()

    def update(self):
        self.t += 1

    def draw(self, screen):
        fonts = self.game.fonts
        screen.blit(self.game.stage, (0, 0))
        screen.blit(self.overlay, (0, 0))

        draw_fighter(screen, 160, 505, 1, CHARACTERS["Blaze"], idle_pose(self.t), scale=1.9)
        draw_fighter(screen, 840, 505, -1, CHARACTERS["Frost"], idle_pose(self.t, 1.7), scale=1.9)

        bounce = math.sin(self.t * 0.05) * 4
        draw_text(screen, "STREET CLASH", fonts["huge"], YELLOW, (WIDTH // 2, 100 + bounce),
                  outline=5, outline_color=(150, 25, 25))
        draw_text(screen, "a two-player fighting game", fonts["small"], WHITE, (WIDTH // 2, 160))
        if (self.t // 30) % 2 == 0:
            draw_text(screen, "PRESS ENTER TO START", fonts["medium"], WHITE, (WIDTH // 2, 222))

        self._draw_controls(screen, pygame.Rect(285, 262, 430, 270))
        draw_text(screen, "ESC  quit      M  mute sound", fonts["tiny"], GREY, (WIDTH // 2, 575))

    def _draw_controls(self, screen, rect):
        fonts = self.game.fonts
        draw_panel(screen, rect, alpha=185, border=(90, 90, 120))
        col_p1, col_p2 = rect.x + 245, rect.x + 360
        draw_text(screen, "CONTROLS", fonts["small"], YELLOW, (rect.x + 20, rect.y + 24), anchor="midleft")
        draw_text(screen, "PLAYER 1", fonts["tiny"], PLAYER_COLORS[1], (col_p1, rect.y + 24))
        draw_text(screen, "PLAYER 2", fonts["tiny"], PLAYER_COLORS[2], (col_p2, rect.y + 24))
        y = rect.y + 55
        for action, (p1_keys, p2_keys) in CONTROL_HINTS.items():
            draw_text(screen, action, fonts["tiny"], WHITE, (rect.x + 20, y), anchor="midleft")
            draw_text(screen, p1_keys, fonts["tiny"], WHITE, (col_p1, y))
            draw_text(screen, p2_keys, fonts["tiny"], WHITE, (col_p2, y))
            y += 25
        draw_text(screen, "Player 2 can also use numpad 1, 2, 4, 5", fonts["tiny"], GREY,
                  (rect.centerx, rect.bottom - 18))


class SelectScreen:
    """Both players pick a fighter and two powers at the same time.

    Each player goes through three steps: "character" -> "powers" -> "ready".
    Punch confirms, kick goes back a step, movement keys move the cursor.
    """
    CHARACTER_COLUMNS = 3
    POWER_COLUMNS = 2
    POWERS_TO_PICK = 2

    def __init__(self, game):
        self.game = game
        self.t = 0
        self.character_names = list(CHARACTERS)
        self.power_names = list(POWERS)

        # The players' choices. This dictionary is handed to the fight.
        self.choices = {
            1: {"character": None, "powers": []},
            2: {"character": None, "powers": []},
        }
        # Which step each player is on and where their cursors are
        self.cursors = {
            1: {"step": "character", "character": 0, "power": 0},
            2: {"step": "character", "character": 1, "power": 0},
        }
        self.background = self._make_background()

    # ------------------------------------------------------------------ input
    def both_ready(self):
        return all(cursor["step"] == "ready" for cursor in self.cursors.values())

    def handle_event(self, event):
        if event.type != pygame.KEYDOWN:
            return
        if event.key == pygame.K_ESCAPE:
            self.game.sounds.play("back")
            self.game.start_title()
            return
        if event.key in START_KEYS and self.both_ready():
            self.start_fight()
            return
        for number in (1, 2):
            for action, keys in CONTROLS[number].items():
                if event.key in keys:
                    self.player_action(number, action)

    def player_action(self, number, action):
        cursor, choice = self.cursors[number], self.choices[number]
        sounds = self.game.sounds
        step = cursor["step"]

        if step == "ready":
            if action == "kick":
                choice["powers"].pop()
                cursor["step"] = "powers"
                sounds.play("back")
            elif action == "punch" and self.both_ready():
                self.start_fight()
            return

        if action in ("left", "right", "up", "down"):
            if step == "character":
                cursor["character"] = self._move(cursor["character"], len(self.character_names),
                                                 self.CHARACTER_COLUMNS, action)
            else:
                cursor["power"] = self._move(cursor["power"], len(self.power_names),
                                             self.POWER_COLUMNS, action)
            sounds.play("move")
            return

        if step == "character" and action == "punch":
            choice["character"] = self.character_names[cursor["character"]]
            cursor["step"] = "powers"
            sounds.play("select")
        elif step == "powers" and action == "punch":
            name = self.power_names[cursor["power"]]
            if name in choice["powers"]:
                choice["powers"].remove(name)
                sounds.play("back")
            else:
                choice["powers"].append(name)
                sounds.play("select")
                if len(choice["powers"]) == self.POWERS_TO_PICK:
                    cursor["step"] = "ready"
                    sounds.play("fight")
        elif step == "powers" and action == "kick":
            if choice["powers"]:
                choice["powers"].pop()
            else:
                choice["character"] = None
                cursor["step"] = "character"
            sounds.play("back")

    @staticmethod
    def _move(index, count, columns, action):
        rows = math.ceil(count / columns)
        row, col = divmod(index, columns)
        if action == "left":
            col = (col - 1) % columns
        elif action == "right":
            col = (col + 1) % columns
        elif action == "up":
            row = (row - 1) % rows
        elif action == "down":
            row = (row + 1) % rows
        return min(row * columns + col, count - 1)

    def start_fight(self):
        self.game.sounds.play("select")
        choices = {number: {"character": choice["character"], "powers": list(choice["powers"])}
                   for number, choice in self.choices.items()}
        self.game.start_fight(choices)

    def update(self):
        self.t += 1

    # ------------------------------------------------------------------ drawing
    @staticmethod
    def _make_background():
        background = pygame.Surface((WIDTH, HEIGHT))
        half = WIDTH // 2
        for y in range(HEIGHT):
            t = y / HEIGHT
            pygame.draw.line(background, blend((25, 40, 90), (8, 10, 30), t), (0, y), (half, y))
            pygame.draw.line(background, blend((90, 25, 35), (30, 8, 12), t), (half, y), (WIDTH, y))
        stripes = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        for i in range(-HEIGHT, WIDTH, 40):
            pygame.draw.line(stripes, (255, 255, 255, 14), (i, HEIGHT), (i + HEIGHT, 0), 8)
        background.blit(stripes, (0, 0))
        pygame.draw.line(background, (90, 90, 120), (half, 60), (half, 535), 2)
        return background

    def _look(self, number, name):
        """Player 2 wears the alternate costume if player 1 already took this fighter."""
        if number == 2 and self.choices[1]["character"] == name:
            return alternate_costume(CHARACTERS[name])
        return CHARACTERS[name]

    @staticmethod
    def _grid(x0, count, columns, width, height, gap_x, gap_y, top=128):
        left = x0 + (WIDTH // 2 - (columns * width + (columns - 1) * gap_x)) // 2
        return [pygame.Rect(left + (i % columns) * (width + gap_x), top + (i // columns) * (height + gap_y),
                            width, height) for i in range(count)]

    def draw(self, screen):
        fonts = self.game.fonts
        screen.blit(self.background, (0, 0))
        draw_text(screen, "CHOOSE YOUR FIGHTERS", fonts["medium"], YELLOW, (WIDTH // 2, 30),
                  outline=3, outline_color=(150, 25, 25))
        draw_text(screen, "ESC  title", fonts["tiny"], GREY, (WIDTH - 12, 14), anchor="topright")

        for number in (1, 2):
            x0 = 0 if number == 1 else WIDTH // 2
            self._draw_header(screen, number, x0)
            step = self.cursors[number]["step"]
            if step == "character":
                self._draw_character_step(screen, number, x0)
            elif step == "powers":
                self._draw_power_step(screen, number, x0)
            else:
                self._draw_ready_step(screen, number, x0)
            self._draw_hints(screen, number, x0)

        if self.both_ready() and (self.t // 25) % 2 == 0:
            banner = pygame.Rect(0, 0, 440, 44)
            banner.center = (WIDTH // 2, 512)
            draw_panel(screen, banner, color=(60, 10, 10), alpha=230, border=YELLOW)
            draw_text(screen, "PRESS ENTER TO FIGHT!", fonts["medium"], YELLOW, banner.center)

    def _draw_header(self, screen, number, x0):
        fonts = self.game.fonts
        choice, step = self.choices[number], self.cursors[number]["step"]
        center = x0 + WIDTH // 4
        title = f"PLAYER {number}"
        if choice["character"]:
            title += f"  -  {choice['character'].upper()}"
        draw_text(screen, title, fonts["medium"], PLAYER_COLORS[number], (center, 76))
        subtitle = {
            "character": "Choose your fighter",
            "powers": f"Choose {self.POWERS_TO_PICK} powers  ({len(choice['powers'])}/{self.POWERS_TO_PICK})",
            "ready": "",
        }[step]
        draw_text(screen, subtitle, fonts["small"], WHITE, (center, 106))

    def _draw_hints(self, screen, number, x0):
        fonts = self.game.fonts
        if number == 1:
            text = "Move  W A S D      Select  F      Back  G"
        else:
            text = "Move  ARROWS      Select  K      Back  L"
        draw_text(screen, text, fonts["tiny"], GREY, (x0 + WIDTH // 4, 568))

    def _platform(self, screen, x, y, number):
        draw_glow(screen, PLAYER_COLORS[number], (x, y - 6), 70, 30)
        rect = pygame.Rect(0, 0, 130, 20)
        rect.center = (x, y)
        pygame.draw.ellipse(screen, blend(PLAYER_COLORS[number], (0, 0, 0), 0.5), rect)
        pygame.draw.ellipse(screen, PLAYER_COLORS[number], rect, 2)

    def _draw_character_step(self, screen, number, x0):
        fonts = self.game.fonts
        cursor, color = self.cursors[number], PLAYER_COLORS[number]
        rects = self._grid(x0, len(self.character_names), self.CHARACTER_COLUMNS, 140, 50, 12, 10)
        for i, (name, rect) in enumerate(zip(self.character_names, rects)):
            info = CHARACTERS[name]
            highlighted = i == cursor["character"]
            draw_panel(screen, rect, color=blend(CARD_COLOR, color, 0.35) if highlighted else CARD_COLOR,
                       alpha=235, border=color if highlighted else (80, 80, 105), width=3 if highlighted else 1)
            pygame.draw.circle(screen, info["gi"], (rect.x + 22, rect.centery), 12)
            pygame.draw.circle(screen, info["trim"], (rect.x + 22, rect.centery), 12, 2)
            draw_text(screen, name.upper(), fonts["small"], WHITE, (rect.x + 42, rect.centery),
                      anchor="midleft")

        name = self.character_names[cursor["character"]]
        info = CHARACTERS[name]
        top = rects[-1].bottom
        facing = 1 if number == 1 else -1
        fighter_x, info_x = (x0 + 110, x0 + 215) if number == 1 else (x0 + 390, x0 + 20)
        self._platform(screen, fighter_x, 500, number)
        draw_fighter(screen, fighter_x, 500, facing, self._look(number, name), idle_pose(self.t, number),
                     scale=1.45)

        draw_text(screen, name.upper(), fonts["medium"], blend(info["gi"], WHITE, 0.35),
                  (info_x, top + 30), anchor="midleft")
        for i, line in enumerate(wrap_text(info["description"], fonts["tiny"], 265)):
            draw_text(screen, line, fonts["tiny"], WHITE, (info_x, top + 58 + i * 19), anchor="midleft",
                      outline=1)
        for i, (label, formula) in enumerate(STAT_BARS.items()):
            y = top + 125 + i * 28
            draw_text(screen, label, fonts["tiny"], GREY, (info_x, y), anchor="midleft", outline=1)
            bar = pygame.Rect(info_x + 80, y - 6, 175, 12)
            amount = max(0.08, min(1.0, formula(info)))
            pygame.draw.rect(screen, (45, 45, 60), bar, border_radius=4)
            pygame.draw.rect(screen, color, (bar.x, bar.y, int(bar.width * amount), bar.height), border_radius=4)

    def _draw_power_step(self, screen, number, x0):
        fonts = self.game.fonts
        cursor, choice, color = self.cursors[number], self.choices[number], PLAYER_COLORS[number]
        rects = self._grid(x0, len(self.power_names), self.POWER_COLUMNS, 216, 40, 12, 8)
        for i, (name, rect) in enumerate(zip(self.power_names, rects)):
            info = POWERS[name]
            highlighted = i == cursor["power"]
            chosen = name in choice["powers"]
            fill = blend(CARD_COLOR, info["color"], 0.45) if chosen else CARD_COLOR
            if highlighted:
                fill = blend(fill, color, 0.25)
            draw_panel(screen, rect, color=fill, alpha=235, radius=8,
                       border=color if highlighted else (80, 80, 105), width=3 if highlighted else 1)
            pygame.draw.circle(screen, info["color"], (rect.x + 17, rect.centery), 8)
            draw_text(screen, name, fonts["tiny"], WHITE, (rect.x + 32, rect.centery), anchor="midleft",
                      outline=1)
            if chosen:
                slot = choice["powers"].index(name) + 1
                badge = pygame.Rect(rect.right - 30, rect.y + 8, 24, 24)
                pygame.draw.rect(screen, WHITE, badge, border_radius=5)
                draw_text(screen, key_label(number, f"power{slot}"), fonts["tiny"], (20, 20, 30),
                          badge.center, outline=0)
            else:
                draw_text(screen, str(info["cost"]), fonts["tiny"], (120, 200, 255),
                          (rect.right - 10, rect.centery), anchor="midright", outline=1)

        # details of the highlighted power
        info = POWERS[self.power_names[cursor["power"]]]
        top = rects[-1].bottom + 12
        box = pygame.Rect(x0 + 25, top, WIDTH // 2 - 50, 100)
        draw_panel(screen, box, alpha=200, border=info["color"])
        draw_text(screen, self.power_names[cursor["power"]], fonts["small"], info["color"],
                  (box.x + 14, box.y + 18), anchor="midleft")
        for i, line in enumerate(wrap_text(info["description"], fonts["tiny"], box.width - 28)):
            draw_text(screen, line, fonts["tiny"], WHITE, (box.x + 14, box.y + 42 + i * 18), anchor="midleft",
                      outline=1)
        draw_text(screen, power_summary(info), fonts["tiny"], (170, 200, 255),
                  (box.x + 14, box.bottom - 14), anchor="midleft", outline=1)

        # the two power slots
        for slot in range(self.POWERS_TO_PICK):
            y = box.bottom + 24 + slot * 28
            picked = choice["powers"][slot] if slot < len(choice["powers"]) else "---"
            draw_text(screen, f"[{key_label(number, f'power{slot + 1}')}]  Power {slot + 1}:  {picked}",
                      fonts["small"], WHITE if picked != "---" else GREY, (x0 + 40, y), anchor="midleft")
        draw_text(screen, "Numbers show the energy cost (max 100)", fonts["tiny"], GREY,
                  (x0 + WIDTH // 4, 535), outline=1)

    def _draw_ready_step(self, screen, number, x0):
        fonts = self.game.fonts
        choice, color = self.choices[number], PLAYER_COLORS[number]
        look = self._look(number, choice["character"])
        facing = 1 if number == 1 else -1
        fighter_x, info_x = (x0 + 125, x0 + 240) if number == 1 else (x0 + 375, x0 + 20)

        draw_text(screen, "READY!", fonts["big"], color, (x0 + WIDTH // 4, 160), outline=3)
        draw_text(screen, f"press {key_label(number, 'kick')} to change", fonts["tiny"], GREY,
                  (x0 + WIDTH // 4, 200), outline=1)
        self._platform(screen, fighter_x, 485, number)
        draw_fighter(screen, fighter_x, 485, facing, look, victory_pose(self.t), scale=1.5)

        draw_text(screen, "POWERS", fonts["small"], YELLOW, (info_x, 300), anchor="midleft")
        for slot, name in enumerate(choice["powers"]):
            power = POWERS[name]
            card = pygame.Rect(info_x, 322 + slot * 54, 235, 44)
            draw_panel(screen, card, color=blend(CARD_COLOR, power["color"], 0.4), alpha=235, radius=8,
                       border=power["color"])
            badge = pygame.Rect(card.x + 8, card.y + 10, 24, 24)
            pygame.draw.rect(screen, WHITE, badge, border_radius=5)
            draw_text(screen, key_label(number, f"power{slot + 1}"), fonts["tiny"], (20, 20, 30),
                      badge.center, outline=0)
            draw_text(screen, name, fonts["small"], WHITE, (card.x + 42, card.centery), anchor="midleft")
