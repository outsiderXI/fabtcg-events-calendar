# FAB Community Event Calendar — GitHub Pages Edition

This project creates a static website plus auto-updating `.ics` subscription feeds from the official Flesh and Blood event search.

## What users can do

The published site lets a visitor:

1. Search for a local game store by **store name, city, state/province, address, or postal code**.
2. Select the store.
3. Subscribe to one permanent feed containing:
   - tracked major/competitive FAB events, **plus**
   - every upcoming FAB event currently listed for that local store.
4. Optionally subscribe to the store-only feed instead.
5. Subscribe to standalone global feeds for Callings, Skirmish, ProQuest, Road to Nationals, Battlegrounds, and other tracked competitive events.

The browser remembers the user's selected store with `localStorage`.

## Why this works on GitHub Pages

GitHub Pages is static and cannot create a new custom calendar at request time.

Instead, the scheduled generator discovers stores from the FAB event search and **pre-generates two feeds per store**:

```text
docs/feeds/stores/<store-id>.ics       # global tracked events + this store
docs/feeds/stores-only/<store-id>.ics  # only this store
```

It also builds:

```text
docs/stores.json
```

The site searches that generated store directory in the browser.

Once a store has been observed in FAB's event listings, it remains in the generated directory even during periods with zero upcoming events. Its permanent `.ics` URL continues to exist, so subscribers do not need to re-add the calendar later.

## Automatic updates

`.github/workflows/update-calendar.yml` runs every six hours and:

1. reads FAB's current Tournament Type options,
2. crawls upcoming event-search pages,
3. normalizes the events,
4. discovers local stores,
5. preserves explicit cancellations for previously listed future events that disappear,
6. rebuilds all global and store feeds,
7. updates the browser store index,
8. commits changed generated files back to `main`.

No manual calendar editing is required.

## Deploy on GitHub

### 1. Create a repository

Create a new GitHub repository, for example:

```text
fab-events-calendar
```

Upload/push the complete contents of this project to the repository root.

### 2. Allow GitHub Actions to write

In the repository:

**Settings → Actions → General → Workflow permissions**

Choose:

**Read and write permissions**

The workflow already declares `contents: write`, but the repository setting must allow it.

### 3. Run the generator once

Go to:

**Actions → Update FAB calendars → Run workflow**

The first successful run populates `docs/stores.json`, global feeds, and all store feeds.

### 4. Turn on GitHub Pages

Go to:

**Settings → Pages**

Set:

- **Source:** Deploy from a branch
- **Branch:** `main`
- **Folder:** `/docs`

Your site will normally appear at:

```text
https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/
```

## Feed examples

Global:

```text
https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/feeds/all.ics
https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/feeds/calling.ics
https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/feeds/skirmish.ics
https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/feeds/proquest.ics
https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/feeds/rtn.ics
https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/feeds/battlegrounds.ics
```

Per-store URLs are generated automatically and are surfaced through the site.

## What counts as a local-store event?

The script learns the current event-type labels from FAB's Tournament Type selector instead of hard-coding season numbers.

Major types such as Callings, Pro Tours, Worlds, and National Championships are not treated as store identities.

Other event types with a store/organizer name in the result heading can populate the local-store directory. This allows Armory, Skirmish, ProQuest, Road to Nationals, Battlegrounds, prerelease, learn-to-play, social play, Super Armory, and similar local events to appear in the selected store's feed as FAB introduces or renames programs.

## Stable store URLs

For store events, FAB's event search links to the store's official locator page, for example:

```text
https://legacy.fabtcg.com/en/locator/roll-the-bones/
```

The generator uses that locator slug as the permanent store ID whenever it is available. This is substantially safer than using the store name or address, and it avoids merging different locations that happen to share a display name.

If a locator URL is unavailable, the generator falls back to a normalized store-name ID.

## Event times

The event search exposes a local clock time, but it does not always expose a reliable IANA timezone alongside it.

For safety, generated events are **all-day calendar entries** and put FAB's published local start time in the description. This avoids silently converting, for example, a 7 PM event in one country to the wrong time in another.

A future backend/geocoding version can convert addresses to IANA timezones and publish true timed events.

## Calendar-client refresh behavior

The generator updates the hosted feed every six hours, but Google Calendar, Apple Calendar, Outlook, and other clients decide when they poll subscription feeds. A publisher cannot force every subscriber's calendar app to refresh immediately.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate

pip install -r requirements.txt
python fab_calendar.py --dry-run
python fab_calendar.py

cd docs
python -m http.server 8000
```

Open `http://localhost:8000`.

## Community / trademark notice

This project should be described as a community resource unless you have authorization from Legend Story Studios.

Flesh and Blood and related marks belong to their respective owners.
