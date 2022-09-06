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

from game_state import GameState
import asyncio
import random
import os
import time

uri = os.environ.get(
    'GAME_CONNECTION_STRING') or "ws://127.0.0.1:3000/?role=agent&agentId=agentId&name=defaultName"


def assign_goals(board, units):
    units = list(units)
    goal_cells = []
    unit_goal_lists = []
    for unit in units:
        unit.set_goal_list()
    while units:
        max_score, max_score_cell, max_score_unit = -1, None, None
        for unit in units:
            for score, cell in unit.goal_list:
                if score > max_score and not cell in goal_cells:
                    max_score, max_score_cell, max_score_unit = score, cell, unit
                    break # Best goal possible for this unit, break out to go to next unit list
        print(f'Unit {max_score_unit.id} goal {max_score_cell.x},{max_score_cell.y}: score={max_score}, dist={max_score_cell.dists[unit.id]}')
        max_score_unit.goal_cell = max_score_cell
        goal_cells.append(max_score_cell)
        units.remove(max_score_unit)


class Agent():
    def __init__(self):
        self._client = GameState(uri)
        self._client.set_game_tick_callback(self._on_game_tick)
        loop = asyncio.get_event_loop()
        connection = loop.run_until_complete(self._client.connect())
        tasks = [asyncio.ensure_future(self._client._handle_messages(connection))]
        loop.run_until_complete(asyncio.wait(tasks))

    async def _on_game_tick(self, board):
        assign_goals(board, board.player.units)
        for unit in board.player.units:
            move_scores = []
            cell = unit.cell
            init_center_dist = abs(cell.x - 7) + abs(cell.y - 7)
            init_goal_dist = unit.goal_cell.dists[unit.id][0]
            for move_cell in cell.move_neighbors():
                goal_dist = init_goal_dist if move_cell is cell else move_cell.get_dist(unit.goal_cell, unit.player)
                center_dist = abs(move_cell.x - 7) + abs(move_cell.y - 7)

                move_score = 0
                if move_cell.blast_powerup or move_cell.freeze_powerup:
                    move_score += 100
                if goal_dist < init_goal_dist:
                    move_score += 10
                if goal_dist > init_goal_dist:
                    move_score -= 10
                #if center_dist > init_center_dist:
                #    move_score += 1
                # TODO: Analyze couple steps of path to determine safety
                if move_cell.future_fire_start and (move_cell.future_fire_start <= board.tick + 1 < move_cell.future_fire_end):
                    move_score -= 1000
                if (move_cell.fire
                    and move_cell.expires > board.tick + 1     # Still on fire next turn
                    and unit.invulnerable <= board.tick + 1):  # Vulnerable next turn
                    move_score -= 10000
                move_scores.append((move_cell, move_score))
                #print(f'Unit {unit.id} move {cell.x},{cell.y} to {move_cell.x},{move_cell.y} dist change from {init_goal_dist} to {goal_dist}. move_score={move_score} (goal at {unit.goal_cell.x},{unit.goal_cell.y})')
            move_scores.sort(key=lambda x: x[1], reverse=True)

            actions = {
                cell: None,
                cell.west:  'left',
                cell.east:  'right',
                cell.north: 'up',
                cell.south: 'down',
            }
            
            for i, (move_cell, move_score) in enumerate(move_scores):
                prefix = '!' if i == 0 else ''
                print(f'{prefix}Unit {unit.id} move {actions[move_cell]}: score={move_score}')

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
