METROLOCAL INDY SOUTH — COLLECTOR

WHAT THIS DOES
--------------
This is intentionally a COLLECTOR, not an editor.

Python gathers.
AI decides what deserves a post.

The recommended setup is now:

    MASTER SCRIPT: every 15 minutes

But the script DOES NOT scrape every source every 15 minutes.

Each source has a configurable refresh interval. Example defaults:

    Sports:                 15 minutes
    Weather:                15 minutes
    News sites:             30 minutes
    Transportation/roads:   30 minutes
    School district/news:   30 minutes
    City/town pages:        120 minutes
    Planning/zoning:        180 minutes
    Councils/agendas:       360 minutes

If the script runs at 8:00 and checks a council page, then runs again at
8:15, 8:30, 8:45, etc., it simply skips that council page until its configured
refresh period has elapsed.

CONFIGURATION
-------------
Normally you edit config.json, NOT the Python file.

The top-level "scheduler" section contains category defaults:

    "default_refresh_minutes_by_category"

Each individual source can override the default with:

    "refresh_minutes": 15

Example:

    {
      "name": "Franklin Central Athletics",
      "url": "https://fcflashes.org/",
      "scope": "southside",
      "category": "sports",
      "refresh_minutes": 15
    }

You can change 15 to 10, 30, 60, 360, etc. whenever you want.

INSTALL
-------
Open Command Prompt / PowerShell in this folder:

    py -m pip install -r requirements.txt

MANUAL RUN
----------
    py metro_local_collector.py

The output is:

    output\latest.json

A timestamped copy is also written each run.

FORCE EVERYTHING TO RUN NOW
---------------------------
If you want to ignore all refresh timers:

    py metro_local_collector.py --force-refresh

If you also want previously-seen items included:

    py metro_local_collector.py --force-refresh --all

STATE FILES
-----------
seen_items.json
    Prevents old stories/items from being repeatedly emitted as new.

last_checked.json
    Remembers when each source was last checked, so a 15-minute master run
    does not hammer every website every 15 minutes.

IMPORTANT BEHAVIOR
------------------
A source that fails is NOT marked as successfully checked. That means it
can retry on the next 15-minute master run instead of waiting several hours.

AUTOMATING WINDOWS
------------------
The simplest Windows setup is Task Scheduler:

1. Create Basic Task
2. Trigger: Daily
3. Repeat task every: 15 minutes
4. Action: Start a program
5. Program/script: py
6. Arguments:
       metro_local_collector.py
7. Start in:
       the folder containing this script

The script itself decides which websites are actually due.

ADDING/REMOVING SITES
---------------------
Add a Southside-specific site:

    {
      "name": "Example Town",
      "url": "https://example.gov/",
      "scope": "southside",
      "category": "town",
      "refresh_minutes": 120
    }

Add a broad site where only Southside matches should survive:

    {
      "name": "Example News",
      "url": "https://example.com/",
      "scope": "broad",
      "category": "news",
      "refresh_minutes": 30
    }

Add/remove locations, schools, roads, neighborhoods, etc. under:

    "broad_source_keywords"

LIMITATIONS
-----------
No generic scraper can guarantee every website forever. Some sites change,
block automated requests, use JavaScript, or put documents inside special
systems.

Failures are recorded in the JSON "errors" section and do not stop the
rest of the collection.

SUGGESTED AI PROMPT
-------------------
Upload output\latest.json and say:

"Act as the editor of a concise Southside Indianapolis local-news account.
Do not summarize everything. Review every item in this JSON, merge duplicate
stories, and choose only items that a meaningful number of Southside residents
would care about. Prioritize major development, roads/closures, restaurants
and recognizable businesses, taxes, schools, school-board decisions, public
safety, major local government decisions, significant sports results and
unusual community news. When in doubt, skip it. For each selected item, give
me a short X/Facebook-ready post and preserve the original source URL."
