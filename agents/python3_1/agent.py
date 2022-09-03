from game_state import GameState
import asyncio
import random
import os
import time

uri = os.environ.get(
    'GAME_CONNECTION_STRING') or "ws://127.0.0.1:3000/?role=agent&agentId=agentId&name=defaultName"

actions = ["up", "down", "left", "right", "bomb", "detonate"]
actions = ["detonate"]


class Agent():
    def __init__(self):
        self._client = GameState(uri)

        # any initialization code can go here
        self._client.set_game_tick_callback(self._on_game_tick)

        loop = asyncio.get_event_loop()
        connection = loop.run_until_complete(self._client.connect())
        tasks = [
            asyncio.ensure_future(self._client._handle_messages(connection)),
        ]
        loop.run_until_complete(asyncio.wait(tasks))

    # returns coordinates of the first bomb placed by a unit
    def _get_bomb_to_detonate(self, unit_id):
        entities = self._client.board.cells
        bombs = list(filter(lambda entity: entity.unit and entity.unit.id == unit_id
            and entity.bomb is not None, entities))
        bomb = next(iter(bombs or []), None)
        if bomb != None:
            return [bomb.x, bomb.y]
        else:
            return None

    async def _on_game_tick(self, board):

        # get my units
        my_units = board.player.units

        # send each unit a random action
        for unit in my_units:

            action = random.choice(actions)

            if action in ["up", "left", "right", "down"]:
                await self._client.send_move(action, unit.id)
            elif action == "bomb":
                await self._client.send_bomb(unit.id)
            elif action == "detonate":
                bomb_coordinates = self._get_bomb_to_detonate(unit.id)
                if bomb_coordinates != None:
                    x, y = bomb_coordinates
                    await self._client.send_detonate(x, y, unit.id)
            else:
                print(f"Unhandled action: {action} for unit {unit.id}")


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
