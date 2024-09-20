#!/usr/bin/env python3
# import asyncio
import os
import random
import sqlite3
from typing import cast

# import aiosqlite
import backoff
import requests
import pandas as pd

import settings as s

NOTHING = "Nothing"

if not os.path.exists("infinite-craft.db"):
    print(
        """There is no infinite-craft.db file!
Make SURE you are in the right directory. To change your directory, use the following command :
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


def are_chars_in_string(chars, string):
    return bool(1 for c in chars if c in string)


@backoff.on_exception(backoff.expo, requests.exceptions.RequestException, max_time=7200)
def combine(combination):
    try:
        response = requests.get(
            "https://neal.fun/api/infinite-craft/pair",
            params={"first": combination[0], "second": combination[1]},
            headers=s.HEADERS,
        )

        if response.status_code == 429:
            raise requests.exceptions.RequestException("Too many requests")
    except Exception as e:
        print("Request exception:", e)
        raise e
    return response.json()


def populate_if_empty():
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


def recipe_exists(df: pd.DataFrame, df_elements: pd.DataFrame, combination: list):
    elem_left = get_element_id(df_elements, combination[0])
    elem_right = get_element_id(df_elements, combination[1])

    return ((df["ingr1"] == elem_left) & (df["ingr2"] == elem_right)).any()


def insert_recipe(
    df: pd.DataFrame,
    df_elements: pd.DataFrame,
    combination: list,
    out_elem: str,
    emoji: str,
    is_new_word: bool,
    is_new_ever: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:

    if is_new_word:
        # Insert into database
        c.execute(
            "INSERT INTO elements (element, emoji, discovered) VALUES (?, ?, ?)",
            (out_elem, emoji, is_new_ever),
        )
        conn.commit()
        rowid = c.lastrowid

        # Append to dataframe
        df_elements_new = pd.DataFrame([[rowid, out_elem]], columns=["id", "element"])
        df_elements = pd.concat([df_elements, df_elements_new])

    id_left = get_element_id(df_elements, combination[0])
    id_right = get_element_id(df_elements, combination[1])
    id_out = get_element_id(df_elements, out_elem)

    try:
        c.execute(
            """INSERT INTO combination (ingr1, ingr2, out) VALUES (?, ?, ?)""",
            (id_left, id_right, id_out),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # We tried to insert a duplicate
        print("Duplicate found: ", combination, out_elem)
        return df, df_elements

    df_new = pd.DataFrame([[id_left, id_right, id_out]], columns=["ingr1", "ingr2", "out"])
    df = pd.concat([df, df_new])

    return df, df_elements


def main():
    checked, newRecipes, newAdditions, firstEvers = 0, 0, 0, 0
    print("Connected!")
    api_gives_info = True

    # Initial population if empty
    populate_if_empty()

    # Fetch current combinations from the database
    df = pd.read_sql_query("SELECT ingr1, ingr2, out FROM combination", conn)
    df_elements = pd.read_sql_query("SELECT id, element FROM elements", conn)
    current = df_elements.loc[df_elements["element"] != NOTHING, "element"].tolist()

    try:
        print("Starting, press CTRL+C or close this window to stop")
        print("Done...")
        while api_gives_info:
            combination = list(sorted((random.choice(current), random.choice(current))))
            checked += 1

            if s.SIMPLE_COMBINES:
                if any(map(are_chars_in_string, s.NON_SIMPLE_CHARS, combination)):
                    continue

            text = f"{combination[0]} + {combination[1]}"

            prefix = "SKIP"
            if recipe_exists(df, df_elements, combination):
                print(f"{prefix:15}: {text}")
                continue

            result = combine(combination)
            if not result:
                continue

            out_elem = result["result"]
            text += " -> " + out_elem

            prefix = "NOTHING"
            is_new_word = False
            if out_elem != NOTHING:
                # We found something
                newRecipes += 1
                prefix = "NEW RECIPE"

                is_new_word = out_elem not in current
                if is_new_word:
                    newAdditions += 1

                    current.append(out_elem)

                    # We didn't know how to craft it
                    prefix = "NEW WORD"

                    if result["isNew"]:
                        firstEvers += 1
                        prefix += " EVER"

            df, df_elements = insert_recipe(
                df,
                df_elements,
                combination,
                out_elem,
                result["emoji"],
                is_new_word,
                result["isNew"],
            )

            print(f"{prefix:15}: {text}")

    except KeyboardInterrupt:
        print("Exiting")
    finally:
        conn.close()

    print(
        f"Checked {checked}. Found {newAdditions} new combinations, {newRecipes} new recipes, and "
        f"{firstEvers} first ever combinations!"
    )


if __name__ == "__main__":
    main()
