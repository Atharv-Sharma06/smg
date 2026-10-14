# Street Clash

A two-player, Street Fighter-style fighting game made with Python and pygame. Both players share
one keyboard. Everything is drawn in code, and the sound effects are generated in code too, so there
are no image or audio files to download.

## Running it

```bash
pip install -r street_fighter/requirements.txt
python street_fighter/main.py
```

## How a game goes

1. **Title screen.** Press ENTER.
2. **Select screen.** Both players choose at the same time, each on their own half of the screen:
   - pick a **fighter** (move the cursor, press your punch key to confirm)
   - pick **2 powers** (powers you've already picked show the key that fires them)
   - your kick key goes back one step
   - once both players are READY, press ENTER to fight
3. **Fight.** Best of three rounds, 60 seconds each. A knockout wins the round. If time runs out,
   whoever has more health left (as a percentage) wins it.
4. **Winner screen.** ENTER for a rematch with the same picks, ESC to choose again.

ESC or P pauses the fight. M mutes the sound anywhere.

## Controls

| Action         | Player 1 | Player 2                |
|----------------|----------|-------------------------|
| Move           | A / D    | Left / Right            |
| Jump           | W        | Up                      |
| Block (crouch) | S        | Down                    |
| Punch          | F        | K (or numpad 1)         |
| Kick           | G        | L (or numpad 2)         |
| Power 1        | R        | I (or numpad 4)         |
| Power 2        | T        | O (or numpad 5)         |

Blocking takes 15% chip damage (chip damage can't knock you out). Thunder Strike can't be blocked.

## Energy and powers

Powers cost **energy**, shown as the blue bar at the bottom of the screen. Energy refills slowly over
time and faster when you land or take hits. After you use a power it has a cooldown before you can
use it again. While it cools down, its button at the bottom of the screen fills back up.

| Power           | What it does                                          | Energy |
|-----------------|-------------------------------------------------------|--------|
| Fireball        | Projectile, 12 damage                                 | 30     |
| Ice Shard       | Fast projectile, 8 damage, slows the target for 2s    | 25     |
| Thunder Strike  | Lightning lands where the enemy stood, unblockable    | 45     |
| Dragon Uppercut | Rising attack that launches the enemy                 | 25     |
| Shadow Dash     | Dashes across the floor, hitting anything in the way  | 20     |
| Healing Light   | Restores 20 health                                    | 50     |
| Iron Shield     | Blocks all damage for 2.5s                            | 35     |
| Berserk Rage    | +50% damage for 5s                                    | 40     |

## The dictionaries

All the game content is stored in dictionaries in [`game_data.py`](game_data.py):

- `CHARACTERS` has each fighter's stats (health, speed, jump, punch, kick, defense) and colours
- `POWERS` has each power's type, damage, cost, cooldown and colour
- `ATTACKS` has the frame data (timing, reach, hitstun) for punches, kicks and attacking powers
- `CONTROLS` maps each player's actions to keys

The players' choices are dictionaries as well. The select screen builds

```python
choices = {
    1: {"character": "Titan", "powers": ["Thunder Strike", "Dragon Uppercut"]},
    2: {"character": "Blaze", "powers": ["Fireball", "Healing Light"]},
}
```

and passes it to the fight. Each `Fighter` keeps its powers in a dictionary keyed by button slot,
and each entry tracks its own cooldown:

```python
fighter.powers = {
    "power1": {"name": "Fireball", "info": POWERS["Fireball"], "cooldown": 0},
    "power2": {"name": "Healing Light", "info": POWERS["Healing Light"], "cooldown": 0},
}
```

**To add a fighter**, add an entry to `CHARACTERS` (copy an existing one and change the numbers and
colours). **To add a power**, add an entry to `POWERS` that uses one of the existing types:
`projectile`, `strike`, `uppercut`, `dash`, `heal`, `shield` or `rage`. New entries show up on the
select screen automatically. The fighter grid has 3 columns and the power grid has 2, so the layout
works best with up to 9 fighters and 8 to 10 powers.

## Files

| File           | What's in it                                                      |
|----------------|-------------------------------------------------------------------|
| `main.py`      | Starts the game and switches between screens                      |
| `game_data.py` | The dictionaries described above                                  |
| `settings.py`  | Window size, physics, round length, colours                       |
| `menus.py`     | Title screen and the character / power select screen              |
| `fight.py`     | Rounds, timer, collisions, HUD, winner screen                     |
| `fighter.py`   | The `Fighter` class: movement, attacks, powers, getting hit       |
| `art.py`       | Draws the fighters (poses are dictionaries of joint angles) and the stage |
| `effects.py`   | Particles, projectiles, lightning and the generated sound effects |
| `ui.py`        | Text and panel helpers                                            |

## Tests

```bash
pip install pytest
python -m pytest street_fighter/tests
```

The tests run headless (no window), so they work over SSH and in CI.
