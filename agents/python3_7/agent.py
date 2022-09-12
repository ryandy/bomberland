'''
Action process order:
 - Bomb placement
 - Bomb detonation
 - Movement

If bomb is placed on cell with fire, it detonates immediately.
If unit1 places bomb next to unit2's bomb at same time unit2 detonates, detonates immediately.
  TODO: worthwhile if can damage 2+ opp units
Stunned units cannot detonate bombs.
  TODO: update future_fire_start for stunned units' bombs

TODO: update future_fire_start for bomb->bomb detonations

Score moves/goals for opponent units too. I also want to go to there.
  e.g. move to same cell opp wants to go to to escape future fire, blocking the move

Maybe do full cell->cell distance calculation?
  - or at least do full dist calculation for each move cell candidate
    - Helpful to see relative distances to target for different move cells

'''

from game_state import GameState, SIZE, UNREACHABLE
import asyncio
import random
import os
import time

uri = os.environ.get(
    'GAME_CONNECTION_STRING') or "ws://127.0.0.1:3000/?role=agent&agentId=agentId&name=defaultName"


import gc
class Agent():
    def __init__(self):
        self._client = GameState(uri)
        self._client.set_game_tick_callback(self._on_game_tick)
        loop = asyncio.get_event_loop()
        connection = loop.run_until_complete(self._client.connect())
        tasks = [asyncio.ensure_future(self._client._handle_messages(connection))]
        loop.run_until_complete(asyncio.wait(tasks))

    async def _on_game_tick(self, board):
        init_score, init_desc = board.get_score(board.player.id)
        locked_actions = []
        
        for unit in board.player.units:
            if unit.hp <= 0 or board.tick <= unit.stunned:
                continue

            best_action = None, -UNREACHABLE, None
            for move_cell in unit.cell.move_neighbors(): # Does not include no-move
                actions = [('move', unit.id, move_cell.x, move_cell.y)]
                board_copy = board.copy()
                board_copy.apply_actions(locked_actions + actions)
                score, desc = board_copy.get_score(unit.player.id)
                #print(f'unit {unit.id} {actions[0]}, score={score}')
                if score > best_action[1]:
                    best_action = actions[0], score, desc
                del board_copy

            # No move does not need to be calculated, just the prev/init score
            #print(f'unit {unit.id} no move, score={init_score}')
            if init_score > best_action[1]:
                best_action = ('move', unit.id, unit.x, unit.y), init_score, init_desc

            for bomb_cell in unit.bombs:
                actions = [('detonate', unit.id, bomb_cell.x, bomb_cell.y)]
                board_copy = board.copy()
                board_copy.apply_actions(locked_actions + actions)
                score, desc = board_copy.get_score(unit.player.id)
                #print(f'unit {unit.id} {actions[0]}, score={score}')
                if score > best_action[1]:
                    best_action = actions[0], score, desc
                del board_copy

            actions = [('bomb', unit.id)]
            board_copy = board.copy()
            board_copy.apply_actions(locked_actions + actions)
            score, desc = board_copy.get_score(unit.player.id)
            #print(f'unit {unit.id} {actions[0]}, score={score}')
            if score > best_action[1]:
                best_action = actions[0], score, desc
            del board_copy

            #print(f'BEST unit {unit.id} {best_action[0]}, score={best_action[1]}')
            #for m, d in best_action[2]:
            #    print(m, d)

            init_score, init_desc = best_action[1], best_action[2] # Update for next unit
            locked_actions.append(best_action[0])
            if best_action[0][0] == 'move':
                action_str = None
                if best_action[0][2] < unit.x:
                    action_str = 'left'
                elif best_action[0][2] > unit.x:
                    action_str = 'right'
                elif best_action[0][3] < unit.y:
                    action_str = 'down'
                elif best_action[0][3] > unit.y:
                    action_str = 'up'
                if action_str:
                    await self._client.send_move(action_str, unit.id)
            elif best_action[0][0] == 'bomb':
                await self._client.send_bomb(unit.id)
            elif best_action[0][0] == 'detonate':
                await self._client.send_detonate(best_action[0][2], best_action[0][3], unit.id)
            else:
                print('bad action:', best_action)
                raise TypeError('')
        gc.collect()


def main():
    for i in range(0,10):
        while True:
            try:
                Agent()
            except:
                time.sleep(5)
                continue
            break


if __name__ == "__main__":
    main()
