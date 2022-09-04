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

'''

from game_state import GameState
import asyncio
import random
import os
import time

uri = os.environ.get(
    'GAME_CONNECTION_STRING') or "ws://127.0.0.1:3000/?role=agent&agentId=agentId&name=defaultName"


class Agent():
    def __init__(self):
        self._client = GameState(uri)
        self._client.set_game_tick_callback(self._on_game_tick)
        loop = asyncio.get_event_loop()
        connection = loop.run_until_complete(self._client.connect())
        tasks = [asyncio.ensure_future(self._client._handle_messages(connection))]
        loop.run_until_complete(asyncio.wait(tasks))

    async def _on_game_tick(self, board):
        for unit in board.player.units:
            cell = unit.cell
            move_scores = []
            init_center_dist = abs(cell.x - 7) + abs(cell.y - 7)
            for move_cell in cell.move_neighbors():
                #print(f'unit {unit.id}: cell ({cell.x},{cell.y}): {cell.wall} {cell.box} {cell.blast_powerup} {cell.freeze_powerup}')
                center_dist = abs(move_cell.x - 7) + abs(move_cell.y - 7)
                move_score = init_center_dist - center_dist
                if move_cell.future_fire_start:
                    move_score -= 10
                if move_cell.fire and unit.invulnerable < move_cell.expires:
                    # TODO: can move if fire expires next tick
                    move_score -= 100
                if move_cell.blast_powerup or move_cell.freeze_powerup:
                    move_score += 5
                move_scores.append((move_cell, move_score))
            move_scores.sort(key=lambda x: x[1], reverse=True)

            move_cell, move_score = move_scores[0]
            action = {
                cell: None,
                cell.west:  'left',
                cell.east:  'right',
                cell.north: 'up',
                cell.south: 'down',
            }[move_cell]

            if action:
                await self._client.send_move(action, unit.id)


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
