"""Particles, floating text, projectiles, lightning strikes and synthesised sound effects."""
import math
import random
from array import array

import pygame

from art import blend, draw_glow
from settings import FPS, GROUND_Y, WHITE, WIDTH
from ui import outlined


class Particle:
    def __init__(self, x, y, vx, vy, color, size, life, gravity=0.0):
        self.x, self.y = x, y
        self.vx, self.vy = vx, vy
        self.color = color
        self.size = size
        self.life = self.max_life = life
        self.gravity = gravity

    @property
    def alive(self):
        return self.life > 0

    def update(self):
        self.x += self.vx
        self.y += self.vy
        self.vy += self.gravity
        self.vx *= 0.95
        self.life -= 1

    def draw(self, surface):
        radius = self.size * self.life / self.max_life
        if radius >= 0.5:
            pygame.draw.circle(surface, self.color, (self.x, self.y), radius)


def burst(particles, x, y, color, count=12, speed=5.0, gravity=0.15, size=5, life=24):
    """Throw out a ring of sparks."""
    for _ in range(count):
        angle = random.uniform(0, math.tau)
        power = random.uniform(speed * 0.3, speed)
        particles.append(Particle(x, y, math.cos(angle) * power, math.sin(angle) * power,
                                  blend(color, WHITE, random.random() * 0.4),
                                  random.uniform(size * 0.5, size), random.randint(life // 2, life), gravity))


class FloatingText:
    """Text that drifts upwards and fades, e.g. damage numbers."""

    def __init__(self, text, x, y, color, font, life=50):
        self.image = outlined(font, text, color, 2)
        self.x, self.y = x, y
        self.life = life

    @property
    def alive(self):
        return self.life > 0

    def update(self):
        self.y -= 0.9
        self.life -= 1

    def draw(self, surface):
        image = self.image
        if self.life < 15:
            image = image.copy()
            image.set_alpha(int(255 * self.life / 15))
        surface.blit(image, image.get_rect(center=(self.x, self.y)))


class Projectile:
    """A fireball / ice shard flying across the stage."""

    def __init__(self, owner, info, x, y, direction, damage):
        self.owner = owner
        self.info = info
        self.color = info["color"]
        self.x, self.y = x, y
        self.vx = info["speed"] * direction
        self.damage = damage
        self.radius = 12 if info.get("freeze") else 16
        self.age = 0
        self.alive = True

    def rect(self):
        r = self.radius
        return pygame.Rect(int(self.x - r), int(self.y - r), r * 2, r * 2)

    def update(self, particles):
        self.x += self.vx
        self.age += 1
        if self.age % 2 == 0:
            particles.append(Particle(self.x - self.vx, self.y + random.uniform(-7, 7), -self.vx * 0.1,
                                      random.uniform(-0.6, 0.6), self.color, random.uniform(3, 7), 18))
        if self.x < -60 or self.x > WIDTH + 60:
            self.alive = False

    def draw(self, surface):
        x, y, r = self.x, self.y, self.radius
        direction = 1 if self.vx > 0 else -1
        draw_glow(surface, self.color, (x, y), r * 2.2, 90)
        if self.info.get("freeze"):
            points = [(x + r * 1.7 * direction, y), (x, y - r * 0.75),
                      (x - r * 1.2 * direction, y), (x, y + r * 0.75)]
            pygame.draw.polygon(surface, blend(self.color, WHITE, 0.5), points)
            pygame.draw.polygon(surface, self.color, points, 2)
        else:
            wobble = 1 + 0.15 * math.sin(self.age * 0.8)
            pygame.draw.circle(surface, self.color, (x, y), r * wobble)
            pygame.draw.circle(surface, blend(self.color, WHITE, 0.5), (x + 2 * direction, y), r * 0.65)
            pygame.draw.circle(surface, WHITE, (x + 4 * direction, y), r * 0.3)


class LightningStrike:
    """Shows a warning on the floor, then a bolt comes down after a short delay."""
    WIDTH = 70
    BOLT_FRAMES = 16

    def __init__(self, owner, info, x, damage):
        self.owner = owner
        self.info = info
        self.x = x
        self.damage = damage
        self.delay = int(info["delay"] * FPS)
        self.timer = 0
        self.alive = True
        self.bolt = []

    @property
    def striking_now(self):
        return self.timer == self.delay

    def hit_rect(self):
        return pygame.Rect(int(self.x - self.WIDTH / 2), 0, self.WIDTH, GROUND_Y)

    def update(self):
        self.timer += 1
        if self.timer >= self.delay and (self.timer - self.delay) % 3 == 0:
            self.bolt = [(self.x + random.uniform(-18, 18), y) for y in range(0, GROUND_Y, GROUND_Y // 10)]
            self.bolt[0] = (self.x, 0)
            self.bolt.append((self.x, GROUND_Y))
        if self.timer > self.delay + self.BOLT_FRAMES:
            self.alive = False

    def draw_warning(self, surface):
        if self.timer >= self.delay:
            return
        pulse = 0.5 + 0.5 * math.sin(self.timer * 0.5)
        width = 50 + 40 * self.timer / self.delay
        rect = pygame.Rect(0, 0, width, 16)
        rect.center = (self.x, GROUND_Y - 2)
        pygame.draw.ellipse(surface, blend((90, 70, 20), self.info["color"], pulse), rect, 3)

    def draw(self, surface):
        if self.timer < self.delay or not self.bolt:
            return
        color = self.info["color"]
        draw_glow(surface, color, (self.x, GROUND_Y - 30), 70, 70)
        pygame.draw.lines(surface, color, False, self.bolt, 9)
        pygame.draw.lines(surface, WHITE, False, self.bolt, 3)


# Sound effects are generated in code so the game needs no audio files.
#   name: (seconds, start pitch Hz, end pitch Hz, noise 0-1, volume 0-1)
SOUND_RECIPES = {
    "punch": (0.08, 220, 60, 0.75, 0.55),
    "kick": (0.13, 160, 40, 0.65, 0.6),
    "swing": (0.07, 900, 400, 0.9, 0.12),
    "block": (0.07, 1200, 900, 0.35, 0.3),
    "fire": (0.30, 160, 520, 0.6, 0.35),
    "zap": (0.40, 90, 40, 0.95, 0.5),
    "power": (0.28, 300, 950, 0.1, 0.25),
    "heal": (0.45, 520, 1040, 0.0, 0.22),
    "ko": (0.90, 420, 50, 0.3, 0.45),
    "move": (0.04, 700, 700, 0.0, 0.15),
    "select": (0.12, 520, 1040, 0.0, 0.22),
    "back": (0.10, 500, 260, 0.0, 0.2),
    "deny": (0.14, 140, 110, 0.2, 0.25),
    "round": (0.50, 330, 330, 0.0, 0.25),
    "fight": (0.45, 440, 880, 0.15, 0.3),
}


class SoundBank:
    def __init__(self):
        self.sounds = {}
        self.muted = False
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            rate, size, channels = pygame.mixer.get_init()
        except (pygame.error, TypeError):
            return  # no audio device: play silently
        if size != -16:
            return
        for name, recipe in SOUND_RECIPES.items():
            self.sounds[name] = self._synth(rate, channels, *recipe)

    @staticmethod
    def _synth(rate, channels, seconds, start_pitch, end_pitch, noise, volume):
        rng = random.Random(start_pitch)
        count = int(rate * seconds)
        samples = array("h")
        phase = 0.0
        for i in range(count):
            t = i / count
            phase += math.tau * (start_pitch + (end_pitch - start_pitch) * t) / rate
            wave = math.sin(phase)
            tone = wave * 0.75 + (0.25 if wave >= 0 else -0.25)
            value = tone * (1 - noise) + rng.uniform(-1, 1) * noise
            envelope = min(1.0, i / (rate * 0.004)) * (1 - t) ** 2
            samples.extend([int(value * envelope * volume * 32767)] * channels)
        return pygame.mixer.Sound(buffer=samples.tobytes())

    def play(self, name):
        if not self.muted and name in self.sounds:
            self.sounds[name].play()

    def toggle_mute(self):
        self.muted = not self.muted
