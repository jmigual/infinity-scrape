#!/usr/bin/env python3
# import asyncio
import os
import random
import sqlite3
from typing import cast
from dataclasses import dataclass

# import aiosqlite
import backoff
import requests
import pandas as pd

import settings as s

NOTHING = "Nothing"

Combination = tuple[tuple[str, int], tuple[str, int]]


class ElementIds:
    def __init__(self):
        self.id_to_element = {}
        self.element_to_id = {}

    def add(self, element: str, id: int):
        self.id_to_element[id] = element
        self.element_to_id[element] = id

    def get_element(self, id: int) -> str:
        return self.id_to_element[id]

    def get_id(self, element: str) -> int:
        return self.element_to_id[element]

    def has_element(self, element: str) -> bool:
        return element in self.element_to_id

    def load_data(self, c: sqlite3.Cursor):
        c.execute("SELECT id, element FROM elements")
        for row in c.fetchall():
            self.add(row[1], row[0])

    def random_pair(self) -> Combination:
        ids = random.sample(list(self.id_to_element.keys()), 2)
        return Combination(
            sorted(((self.get_element(ids[0]), ids[0]), (self.get_element(ids[1]), ids[1])))
        )


@dataclass
class History:
    checked: int = 0
    new_recipes: int = 0
    new_additions: int = 0
    first_evers: int = 0

    def __str__(self) -> str:
        return (
            f"Checked: {self.checked}\n"
            f"New recipes: {self.new_recipes}\n"
            f"New additions: {self.new_additions}\n"
            f"First evers: {self.first_evers}"
        )


def are_chars_in_string(string):
    chars = s.NON_SIMPLE_CHARS
    return bool(1 for c in chars if c in string)


def on_backoff(details):
    print(
        f"Backing off {details['wait']} seconds after {details['tries']} tries due to "
        f"{details['exception']}"
    )


@backoff.on_exception(
    backoff.expo, requests.exceptions.RequestException, max_time=7200, on_backoff=on_backoff
)
def combine(combination):
    try:
        response = requests.get(
            "https://neal.fun/api/infinite-craft/pair",
            params={"first": combination[0][0], "second": combination[1][0]},
            headers=s.HEADERS,
        )

        if response.status_code == 429:
            raise requests.exceptions.RequestException("Too many requests")
        elif response.status_code == 403:
            # Sometimes it returns 403 if we go too fast
            raise requests.exceptions.RequestException("Forbidden")
            # print(f"Forbidden {' + '.join(c[0] for c in combination)}")
            # return None
        elif response.status_code == 500:
            # Ignore server errors
            return None
        response.raise_for_status()
    except Exception as e:
        print("Request exception:", e)
        raise e
    return response.json()


def populate_if_empty(c: sqlite3.Cursor, conn: sqlite3.Connection):
    c.execute("SELECT COUNT(*) FROM elements")
    if c.fetchone()[0] == 0:
        elements = [
            (NOTHING, "", False),
            ("Water", "💧", False),
            ("Fire", "🔥", False),
            ("Wind", "🌬️", False),
            ("Earth", "🌍", False),
        ]
        print("Populating elements")
        c.executemany(
            "INSERT INTO elements (element, emoji, discovered) VALUES (?, ?, ?)", elements
        )
        conn.commit()


def get_element_id(df_elements: pd.DataFrame, element: str):
    return cast(
        pd.Series, df_elements.loc[df_elements["element"] == element, "id"]
    ).values.tolist()[0]


def recipe_exists(df: pd.DataFrame, combination: Combination):
    id_left = combination[0][1]
    id_right = combination[1][1]

    return ((df["ingr1"] == id_left) & (df["ingr2"] == id_right)).any()


def insert_recipe(
    df: pd.DataFrame,
    current: ElementIds,
    combination: Combination,
    out_elem: str,
    emoji: str,
    is_new_word: bool,
    is_new_ever: bool,
    c: sqlite3.Cursor,
    conn: sqlite3.Connection,
) -> tuple[pd.DataFrame, ElementIds]:

    if is_new_word:
        # Insert into database
        c.execute(
            "INSERT INTO elements (element, emoji, discovered) VALUES (?, ?, ?)",
            (out_elem, emoji, is_new_ever),
        )
        conn.commit()
        rowid = c.lastrowid

        # Append to current
        if rowid is None:
            raise ValueError("rowid is None")
        current.add(out_elem, rowid)

    id_left: int = combination[0][1]
    id_right: int = combination[1][1]
    id_out: int = current.get_id(out_elem)

    try:
        c.execute(
            """INSERT INTO combination (ingr1, ingr2, out) VALUES (?, ?, ?)""",
            (id_left, id_right, id_out),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # We tried to insert a duplicate
        print("Duplicate found: ", combination, out_elem)
        return df, current

    df_new = pd.DataFrame([[id_left, id_right, id_out]], columns=["ingr1", "ingr2", "out"])
    df = pd.concat([df, df_new])

    return df, current


def search_loop(history: History, c: sqlite3.Cursor, conn: sqlite3.Connection):
    print("Connected!")
    api_gives_info = True

    # Initial population if empty
    populate_if_empty(c, conn)

    # Fetch current combinations from the database
    df = pd.read_sql_query("SELECT ingr1, ingr2, out FROM combination", conn)

    current = ElementIds()
    current.load_data(c)

    print("Starting, press CTRL+C or close this window to stop")
    print("Done...")
    while api_gives_info:
        combination = current.random_pair()
        history.checked += 1

        if s.SIMPLE_COMBINES:
            if any(map(are_chars_in_string, [combination[0][0], combination[1][0]])):
                continue

        text = f"{combination[0][0]} + {combination[1][0]}"

        prefix = "SKIP"
        if recipe_exists(df, combination):
            print(f"{prefix:15}: {text}")
            continue

        result = combine(combination)
        if not result or result is None:
            prefix = "No result"
            print(f"{prefix:15}: {text}")
            continue

        out_elem: str = result["result"]
        text += " -> " + out_elem

        prefix = "NOTHING"
        is_new_word = False
        if out_elem != NOTHING:
            # We found something
            history.new_recipes += 1
            prefix = "NEW RECIPE"

            is_new_word = not current.has_element(out_elem)
            if is_new_word:
                history.new_additions += 1

                # We didn't know how to craft it
                prefix = "NEW WORD"

                if result["isNew"]:
                    history.first_evers += 1
                    prefix += " EVER"

        df, current = insert_recipe(
            df,
            current,
            combination,
            out_elem,
            result["emoji"],
            is_new_word,
            result["isNew"],
            c,
            conn,
        )

        print(f"{prefix:15}: {text}")


def main():
    if not os.path.exists("infinite-craft.db"):
        print(
            """There is no infinite-craft.db file!
        Make SURE you are in the right directory. Change your directory using the following command:
        cd path/to/infinite-craft.db
        (Right click on infinite-craft.db to copy file path)"""
        )
        if (
            not input(
                "Are you sure you want to start a new database? "
                "You can use the one on the github page. [Y/n] "
            )
            .lower()
            .startswith("y")
        ):
            exit(1)
    print("Connecting to database...")
    conn = sqlite3.connect("infinite-craft.db")
    c = conn.cursor()

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS "combination" (
        "id"	INTEGER NOT NULL,
        "ingr1"	INTEGER FOREIGNKEY REFERENCES elements(id),
        "ingr2"	INTEGER FOREIGNKEY REFERENCES elements(id),
        "out"	INTEGER FOREIGNKEY REFERENCES elements(id),
        PRIMARY KEY("id"),
        UNIQUE("ingr1","ingr2","out")
    )
    """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS "elements" (
        "id"      INTEGER NOT NULL,
        "element" TEXT,
        "emoji"   TEXT,
        "discovered" BOOLEAN,
        PRIMARY KEY("id"),
        UNIQUE("element")
    )
    """
    )

    c.execute(
        """
        CREATE VIEW IF NOT EXISTS combination_readable AS
        SELECT e1.element as ingr1, e2.element as ingr2, e3.element as out
        FROM combination AS c
        JOIN elements e1 ON c.ingr1 = e1.id
        JOIN elements e2 ON c.ingr2 = e2.id
        JOIN elements e3 ON c.out = e3.id
        ORDER BY out
    """
    )

    history = History()
    try:
        search_loop(history, c, conn)
    except KeyboardInterrupt:
        print("Exiting")
    except Exception as e:
        print("An error occurred:", e)
    finally:
        conn.close()

    print(history)


if __name__ == "__main__":
    main()
