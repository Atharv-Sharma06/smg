"""Street Clash - a two-player Street Fighter style game made with pygame.

Run it with:   python main.py
"""
import pygame

from art import make_stage
from effects import SoundBank
from fight import FightScreen
from menus import SelectScreen, TitleScreen
from settings import FPS, HEIGHT, TITLE, WIDTH


class Game:
    """Owns the window and switches between the title, select and fight screens."""

    def __init__(self):
        pygame.mixer.pre_init(22050, -16, 1, 512)
        pygame.init()
        pygame.display.set_caption(TITLE)
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT))
        self.clock = pygame.time.Clock()
        self.fonts = {
            "huge": pygame.font.Font(None, 120),
            "big": pygame.font.Font(None, 80),
            "medium": pygame.font.Font(None, 44),
            "small": pygame.font.Font(None, 30),
            "tiny": pygame.font.Font(None, 22),
        }
        self.sounds = SoundBank()
        self.stage = make_stage()
        self.running = True
        self.scene = TitleScreen(self)

    # Screen changes
    def start_title(self):
        self.scene = TitleScreen(self)

    def start_select(self):
        self.scene = SelectScreen(self)

    def start_fight(self, choices):
        self.scene = FightScreen(self, choices)

    def quit(self):
        self.running = False

    def run(self):
        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_m:
                    self.sounds.toggle_mute()
                else:
                    self.scene.handle_event(event)
            self.scene.update()
            self.scene.draw(self.screen)
            pygame.display.flip()
            self.clock.tick(FPS)
        pygame.quit()


if __name__ == "__main__":
    Game().run()
