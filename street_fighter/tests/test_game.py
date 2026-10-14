from collections import defaultdict

import pygame
import pytest

from fight import FightScreen
from fighter import Fighter
from game_data import CHARACTERS, CONTROLS, POWERS
from main import Game
from menus import SelectScreen

POWER_TYPES = {"projectile", "strike", "uppercut", "dash", "heal", "shield", "rage"}
NO_KEYS = defaultdict(bool)


class FakeWorld:
    """The bits of FightScreen a Fighter talks to."""

    def __init__(self):
        self.projectiles, self.strikes = [], []
        self.combat_active = True

    def play(self, name): pass
    def burst(self, *args, **kwargs): pass
    def float_text(self, *args, **kwargs): pass
    def hitstop(self, frames): pass
    def shake(self, amount): pass


@pytest.fixture(scope="module")
def game():
    return Game()


def press(scene, key):
    scene.handle_event(pygame.event.Event(pygame.KEYDOWN, key=key))


def test_data_dictionaries_are_complete():
    for name, info in CHARACTERS.items():
        for key in ("description", "health", "speed", "jump", "punch", "kick", "defense",
                    "gi", "trim", "skin", "hair"):
            assert key in info, f"{name} is missing {key}"
    for name, info in POWERS.items():
        assert info["type"] in POWER_TYPES, name
        for key in ("description", "cost", "cooldown", "color"):
            assert key in info, f"{name} is missing {key}"
    p1_keys = {k for keys in CONTROLS[1].values() for k in keys}
    p2_keys = {k for keys in CONTROLS[2].values() for k in keys}
    assert not p1_keys & p2_keys, "players must not share keys"


def test_select_screen_stores_choices_and_starts_fight(game):
    scene = SelectScreen(game)
    game.scene = scene
    press(scene, pygame.K_f)                        # P1: Blaze
    press(scene, pygame.K_f)                        # P1: Fireball
    press(scene, pygame.K_d)
    press(scene, pygame.K_f)                        # P1: Ice Shard
    press(scene, pygame.K_k)                        # P2: Titan
    press(scene, pygame.K_DOWN)
    press(scene, pygame.K_k)                        # P2: Thunder Strike
    press(scene, pygame.K_DOWN)
    press(scene, pygame.K_DOWN)
    press(scene, pygame.K_k)                        # P2: Iron Shield
    assert scene.choices == {
        1: {"character": "Blaze", "powers": ["Fireball", "Ice Shard"]},
        2: {"character": "Titan", "powers": ["Thunder Strike", "Iron Shield"]},
    }
    assert scene.both_ready()

    press(scene, pygame.K_RETURN)
    assert isinstance(game.scene, FightScreen)
    p1 = game.scene.fighters[1]
    assert p1.character == "Blaze"
    assert {slot: power["name"] for slot, power in p1.powers.items()} == {"power1": "Fireball",
                                                                          "power2": "Ice Shard"}


def test_select_back_undoes_one_step(game):
    scene = SelectScreen(game)
    for key in (pygame.K_f, pygame.K_f, pygame.K_d, pygame.K_f):
        press(scene, key)
    assert scene.cursors[1]["step"] == "ready"
    press(scene, pygame.K_g)
    assert scene.cursors[1]["step"] == "powers" and scene.choices[1]["powers"] == ["Fireball"]
    press(scene, pygame.K_g)
    press(scene, pygame.K_g)
    assert scene.cursors[1]["step"] == "character" and scene.choices[1]["character"] is None


def test_punch_hits_and_blocking_reduces_damage():
    world = FakeWorld()
    attacker = Fighter(1, "Blaze", ["Fireball", "Ice Shard"], 400, 1)
    defender = Fighter(2, "Blaze", ["Fireball", "Ice Shard"], 455, -1)
    attacker.press(pygame.K_f)
    for _ in range(12):
        attacker.update(NO_KEYS, defender, world, True)
        defender.update(NO_KEYS, attacker, world, False)
    assert defender.health == pytest.approx(100 - CHARACTERS["Blaze"]["punch"])
    assert defender.state == "hit"

    blocker = Fighter(2, "Blaze", ["Fireball", "Ice Shard"], 455, -1)
    blocker.set_state("crouch")
    assert blocker.receive_hit(10, 400, 16, 4, world) == "block"
    assert blocker.health == pytest.approx(98.5)


def test_powers_cost_energy_and_have_cooldowns():
    world = FakeWorld()
    fighter = Fighter(1, "Frost", ["Fireball", "Healing Light"], 300, 1)
    opponent = Fighter(2, "Viper", ["Fireball", "Ice Shard"], 700, -1)
    fighter.energy = 100
    assert fighter.use_power("power1", world)
    assert fighter.energy == 70 and fighter.powers["power1"]["cooldown"] > 0
    assert not fighter.use_power("power1", world)            # still cooling down
    for _ in range(10):
        fighter.update(NO_KEYS, opponent, world, False)
    assert len(world.projectiles) == 1

    fighter.energy = 10
    assert not fighter.use_power("power2", world)            # not enough energy


def test_heal_shield_and_freeze():
    world = FakeWorld()
    fighter = Fighter(1, "Titan", ["Healing Light", "Iron Shield"], 300, 1)
    opponent = Fighter(2, "Viper", ["Fireball", "Ice Shard"], 700, -1)
    fighter.health = 125
    fighter.energy = 100
    fighter.use_power("power1", world)
    for _ in range(10):
        fighter.update(NO_KEYS, opponent, world, False)
    assert fighter.health == fighter.max_health               # heal never goes over max

    fighter.powers["power2"]["cooldown"] = 0
    fighter.energy = 100
    fighter.set_state("idle")
    fighter.use_power("power2", world)
    for _ in range(10):
        fighter.update(NO_KEYS, opponent, world, False)
    assert fighter.receive_hit(20, 700, 16, 4, world) == "shield"
    assert fighter.health == fighter.max_health

    opponent.receive_hit(5, 300, 16, 4, world, freeze=2.0)
    assert opponent.effects["freeze"] > 0
    assert opponent.speed == CHARACTERS["Viper"]["speed"] / 2


def test_match_is_best_of_three(game):
    game.start_fight({1: {"character": "Rocco", "powers": ["Fireball", "Shadow Dash"]},
                      2: {"character": "Shade", "powers": ["Thunder Strike", "Berserk Rage"]}})
    scene = game.scene
    world = FakeWorld()
    for expected_wins in (1, 2):
        while scene.phase != "fight":
            scene.update()
        scene.fighters[2].receive_hit(500, 0, 10, 1, world)
        while scene.phase == "fight":
            scene.update()
        assert scene.wins[1] == expected_wins
        while scene.phase == "round_over":
            scene.update()
    assert scene.phase == "match_over" and scene.champion == 1

    press(scene, pygame.K_RETURN)                           # rematch keeps the same choices
    assert game.scene is not scene and game.scene.choices == scene.choices
