import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import time
from datetime import datetime, timedelta
import re

# ==========================================
# CONFIGURATION
# ==========================================

# Your specific Google Sheet ID
SPREADSHEET_ID = "1sy2axdA1RmUZ89cdUqnLFWXqlckBjwS_xxeuc_Mhpcc"
FIXTURES_SHEET_NAME = "FIFA World Cup 2026 Fixtures"
GROUPS_SHEET_NAME = "Group Teams"
TOP_SCORERS_SHEET_NAME = "Top Scorers"

# ESPN API endpoint for FIFA World Cup Scoreboard
ESPN_API_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"

def get_google_sheet(sheet_name):
    """Authenticates with Google and returns a worksheet object."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)

def update_live_scores():
    """Fetches live scores from ESPN API and updates the Fixtures sheet."""
    print("➡️ Step 1: Fetching live scores from ESPN API...")

    # Add headers to prevent ESPN from blocking the automated script
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    live_matches = []
    
    # Fetch a rolling 4-day window (Past 2 days, Today, Tomorrow) to catch delayed stats and timezone overlaps
    for i in range(-2, 2):
        target_date = (datetime.now() + timedelta(days=i)).strftime("%Y%m%d")
        request_url = f"{ESPN_API_URL}?dates={target_date}"
        
        try:
            response = requests.get(request_url, headers=headers, timeout=10)
            response.raise_for_status()
            live_matches.extend(response.json().get('events', []))
        except Exception as e:
            print(f"⚠️ Could not fetch API data for {target_date}: {e}")
            
    # Deduplicate matches in case ESPN returns the same match on overlapping days
    seen_ids = set()
    unique_matches = []
    for match in live_matches:
        if match.get('id') not in seen_ids:
            unique_matches.append(match)
            seen_ids.add(match.get('id'))
            
    live_matches = unique_matches

    if not live_matches:
        print("⚠️ No live match data found from ESPN.")
        return

    print(f"✅ Found {len(live_matches)} matches from the API.")

    fixtures_sheet = get_google_sheet(FIXTURES_SHEET_NAME)
    all_rows = fixtures_sheet.get_all_records() # Easier to work with headers

    if not all_rows:
        print("⚠️ Fixtures sheet is empty.")
        return
        
    sheet_headers = list(all_rows[0].keys())
    if "Status" not in sheet_headers:
        print("⚠️ Error: The 'Status' column is missing from your Fixtures sheet header row!")
        return
        
    home_col = sheet_headers.index("Home Score") + 1
    away_col = sheet_headers.index("Away Score") + 1
    status_col = sheet_headers.index("Status") + 1
    scorers_col = sheet_headers.index("Goal Scorers") + 1 if "Goal Scorers" in sheet_headers else None
    cards_col = sheet_headers.index("Cards") + 1 if "Cards" in sheet_headers else None

    updates = []
    # ESPN to Google Sheet specific name mappings
    TEAM_MAPPING = {
        "USA": "United States",
        "Korea Republic": "South Korea",
        "Bosnia and Herzegovina": "Bosnia-Herzegovina",
        "Bosnia-Herzegovina": "Bosnia-Herzegovina",
        "Czech Republic": "Czechia",
        "Turkey": "Türkiye",
        "Turkiye": "Türkiye",
        "DR Congo": "Congo DR",
        "Democratic Republic of the Congo": "Congo DR",
        "Democratic Republic of Congo": "Congo DR",
        "Ivory Coast": "Côte d'Ivoire",
        "Cape Verde": "Cabo Verde"
    }

    for row_index, row in enumerate(all_rows, start=2): # start=2 for 1-based index + header
        sheet_matchup = row.get("Matchup", "")
        
        for event in live_matches:
            try:
                competitors = event['competitions'][0]['competitors']
                home_competitor = next(c for c in competitors if c['homeAway'] == 'home')
                away_competitor = next(c for c in competitors if c['homeAway'] == 'away')
                
                espn_home_team = home_competitor['team']['displayName']
                espn_away_team = away_competitor['team']['displayName']
                
                home_team = TEAM_MAPPING.get(espn_home_team, espn_home_team)
                away_team = TEAM_MAPPING.get(espn_away_team, espn_away_team)
            except (KeyError, IndexError, StopIteration):
                continue
            
            if not home_team or not away_team:
                continue
            
            # Match teams based on names appearing in the "Matchup" string
            if home_team in sheet_matchup and away_team in sheet_matchup:
                
                home_score = home_competitor.get('score')
                away_score = away_competitor.get('score')
                
                status_obj = event.get('status', {}).get('type', {})
                status = status_obj.get('shortDetail', 'Live')
                
                event_id = event.get('id')
                
                key_events = []
                summary_data = {}
                
                # Only fetch play-by-play summary if the match has actually started.
                # ESPN uses 'pre' to indicate an upcoming match.
                if event_id and status_obj.get('state') != 'pre':
                    base_api = ESPN_API_URL.split('?')[0].replace('/scoreboard', '')
                    urls_to_try = []
                    
                    # 1. Grab the exact API link if ESPN provides it natively
                    for link in event.get('links', []):
                        rel = link.get('rel', [])
                        if 'api' in rel and 'summary' in rel:
                            urls_to_try.append(link.get('href', '').replace('http://', 'https://'))
                            break
                            
                    # 2. Add our fallbacks for World Cup, Friendlies, and European Qualifiers
                    urls_to_try.extend([
                        f"{base_api}/summary?event={event_id}",
                        f"https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.friendly/summary?event={event_id}",
                        f"https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.euro_q/summary?event={event_id}",
                        f"https://site.api.espn.com/apis/site/v2/sports/soccer/all/summary?event={event_id}"
                    ])
                    
                    for url in urls_to_try:
                        try:
                            summary_resp = requests.get(url, headers=headers, timeout=10)
                            if summary_resp.status_code == 200:
                                summary_data = summary_resp.json()
                                break
                        except Exception as e:
                            pass
                            
                # Extract penalty shootout scores utilizing both event and summary data
                pen_h = home_competitor.get('shootoutScore')
                pen_a = away_competitor.get('shootoutScore')

                if summary_data and 'header' in summary_data:
                    try:
                        h_comps = summary_data['header']['competitions'][0]['competitors']
                        hc = next(c for c in h_comps if c['homeAway'] == 'home')
                        ac = next(c for c in h_comps if c['homeAway'] == 'away')
                        if pen_h is None: pen_h = hc.get('shootoutScore')
                        if pen_a is None: pen_a = ac.get('shootoutScore')
                    except Exception:
                        pass

                if (pen_h is None or pen_a is None) and summary_data and 'shootout' in summary_data:
                    shootout_plays = summary_data.get('shootout', [])
                    if shootout_plays:
                        pen_h, pen_a = 0, 0
                        for play in shootout_plays:
                            is_goal = play.get('scoringPlay') == True or 'goal' in str(play.get('type', {}).get('text', '')).lower()
                            if is_goal:
                                t_id = str(play.get('team', {}).get('id', ''))
                                if t_id == str(home_competitor['team']['id']): pen_h += 1
                                elif t_id == str(away_competitor['team']['id']): pen_a += 1

                if pen_h is not None: pen_h = int(pen_h)
                if pen_a is not None: pen_a = int(pen_a)

                detail_text = status_obj.get('detail', '')
                is_shootout = 'penalt' in detail_text.lower() or 'pens' in status.lower()
                is_tied = (str(home_score) == str(away_score)) if home_score is not None and away_score is not None else False
                
                if not is_shootout and is_tied and (home_competitor.get('winner') or away_competitor.get('winner')):
                    is_shootout = True

                if (pen_h is None or pen_a is None) and is_shootout:
                    match = re.search(r'(\d+)\s*-\s*(\d+)', detail_text)
                    if match:
                        score1, score2 = match.groups()
                        s_win = max(int(score1), int(score2))
                        s_lose = min(int(score1), int(score2))
                        
                        if home_competitor.get('winner'):
                            pen_h, pen_a = s_win, s_lose
                        elif away_competitor.get('winner'):
                            pen_h, pen_a = s_lose, s_win
                        else:
                            pen_h, pen_a = int(score1), int(score2)
                    else:
                        h_lines = home_competitor.get('linescores', [])
                        a_lines = away_competitor.get('linescores', [])
                        if len(h_lines) >= 3 and len(a_lines) >= 3:
                            pen_h = int(h_lines[-1].get('value', 0))
                            pen_a = int(a_lines[-1].get('value', 0))

                if pen_h is not None and pen_a is not None and pen_h != pen_a:
                    if not re.search(r'\d+\s*-\s*\d+', status):
                        status = f"{status} ({pen_h}-{pen_a} pens)"
                elif is_tied and (home_competitor.get('winner') or away_competitor.get('winner')):
                    if "win pens" not in status.lower() and not re.search(r'\d+\s*-\s*\d+', status):
                        w_side = "Home" if home_competitor.get('winner') else "Away"
                        status = f"{status} ({w_side} Win Pens)"

                # Extract Goal Scorers and Cards
                goal_scorers = []
                cards = []

                if summary_data:
                    # 1. Try 'keyEvents' (standard for live big matches)
                    key_events = summary_data.get('keyEvents', [])
                    
                    # 2. Try 'plays' (contains every play, we'll filter it later)
                    if not key_events and 'plays' in summary_data:
                        key_events = summary_data.get('plays', [])
                        
                    # 3. Try 'header' details (older archived matches)
                    if not key_events and 'header' in summary_data:
                        try:
                            key_events = summary_data['header']['competitions'][0].get('details', [])
                        except (KeyError, IndexError):
                            pass
                        
                # 4. Fallback to scoreboard payload
                if not key_events:
                    key_events = event['competitions'][0].get('details', [])
                    
                for detail in key_events:
                    detail_type = str(detail.get('type', {}).get('text') or '').lower()
                    
                    is_goal = detail.get('scoringPlay') == True
                    is_red = detail.get('redCard') == True
                    is_yellow = detail.get('yellowCard') == True
                    
                    if not (is_goal or is_red or is_yellow):
                        if 'goal' in detail_type or 'penalty' in detail_type: is_goal = True
                        elif 'red' in detail_type: is_red = True
                        elif 'yellow' in detail_type: is_yellow = True
                        
                    if not (is_goal or is_red or is_yellow):
                        continue
                        
                    athlete_name = ""
                    if 'participants' in detail and detail['participants']:
                        athlete = detail['participants'][0].get('athlete', {})
                        athlete_name = athlete.get('shortName', athlete.get('displayName', ''))
                    elif 'athletesInvolved' in detail and detail['athletesInvolved']:
                        athlete = detail['athletesInvolved'][0]
                        athlete_name = athlete.get('shortName', athlete.get('displayName', ''))
                    elif 'athlete' in detail:
                        athlete = detail['athlete']
                        athlete_name = athlete.get('shortName', athlete.get('displayName', ''))
                        
                    clock = str(detail.get('clock', {}).get('displayValue', '')).strip()
                    if clock and not clock.endswith("'"):
                        clock += "'"
                        
                    short_text = detail.get('shortText', detail.get('text', ''))
                    
                    # Robust fallback: If athlete name is missing but ESPN gives a text string, use it
                    if not athlete_name and short_text:
                        if is_goal: goal_scorers.append(short_text)
                        elif is_red: cards.append(f"🟥 {short_text}")
                        elif is_yellow: cards.append(f"🟨 {short_text}")
                        continue
                        
                    if not athlete_name:
                        continue
                        
                    time_str = f" - {clock}" if clock else ""
                    
                    if is_goal:
                        goal_scorers.append(f"{athlete_name}{time_str}")
                    elif is_red:
                        cards.append(f"🟥 {athlete_name}{time_str}")
                    elif is_yellow:
                        cards.append(f"🟨 {athlete_name}{time_str}")
                        
                goal_str = ", ".join(goal_scorers)
                card_str = ", ".join(cards)

                # Prepare batch updates to avoid hitting API rate limits
                if home_score is not None and str(row.get("Home Score")) != str(home_score):
                    updates.append(gspread.Cell(row_index, home_col, str(home_score)))
                if away_score is not None and str(row.get("Away Score")) != str(away_score):
                    updates.append(gspread.Cell(row_index, away_col, str(away_score)))
                if status and str(row.get("Status")) != str(status):
                    updates.append(gspread.Cell(row_index, status_col, str(status)))
                if scorers_col and str(row.get("Goal Scorers", "")) != goal_str:
                    updates.append(gspread.Cell(row_index, scorers_col, goal_str))
                if cards_col and str(row.get("Cards", "")) != card_str:
                    updates.append(gspread.Cell(row_index, cards_col, card_str))
                
                break # Move to the next row in the sheet

    if updates:
        print(f"🔄 Found {len(updates)} cells to update in the Fixtures sheet. Applying changes...")
        fixtures_sheet.update_cells(updates)
        print("✅ Fixtures sheet updated successfully!")
    else:
        print("👍 Fixtures sheet is already up-to-date.")

def calculate_and_update_standings():
    """Reads the fixtures sheet, calculates group standings, and updates the groups sheet IN-PLACE."""
    print("\n➡️ Step 2: Calculating group standings from fixture results...")
    
    fixtures_sheet = get_google_sheet(FIXTURES_SHEET_NAME)
    matches = fixtures_sheet.get_all_records()

    # 1. Calculate the points based purely on match results
    team_stats = {}
    for match in matches:
        status = str(match.get("Status", "")).strip().upper()
        # Only calculate points for finished matches
        if status not in ['FT', 'FINISHED', 'FULL TIME', 'FULL-TIME', 'MATCH FINISHED']: 
            continue

        matchup = str(match.get("Matchup", ""))
        home_score = match.get("Home Score")
        away_score = match.get("Away Score")

        if not matchup or str(home_score).strip() == "" or str(away_score).strip() == "":
            continue

        try:
            home_score = int(home_score)
            away_score = int(away_score)
            
            # Handle both " vs " and " v " automatically
            separator = ' vs ' if ' vs ' in matchup.lower() else (' v ' if ' v ' in matchup.lower() else 'vs')
            teams = [t.strip() for t in matchup.split(separator)]
            home_team, away_team = teams[0], teams[1]
        except (ValueError, IndexError):
            continue

        # Make sure both teams exist in our tracking dictionary
        for team in [home_team, away_team]:
            if team not in team_stats:
                team_stats[team] = {'MP': 0, 'W': 0, 'D': 0, 'L': 0, 'GF': 0, 'GA': 0, 'Pts': 0, 'Form': []}

        # Tally the match
        team_stats[home_team]['MP'] += 1
        team_stats[away_team]['MP'] += 1
        team_stats[home_team]['GF'] += home_score
        team_stats[home_team]['GA'] += away_score
        team_stats[away_team]['GF'] += away_score
        team_stats[away_team]['GA'] += home_score

        if home_score > away_score:
            team_stats[home_team]['W'] += 1
            team_stats[home_team]['Pts'] += 3
            team_stats[away_team]['L'] += 1
            team_stats[home_team]['Form'].append('W')
            team_stats[away_team]['Form'].append('L')
        elif away_score > home_score:
            team_stats[away_team]['W'] += 1
            team_stats[away_team]['Pts'] += 3
            team_stats[home_team]['L'] += 1
            team_stats[away_team]['Form'].append('W')
            team_stats[home_team]['Form'].append('L')
        else:
            team_stats[home_team]['D'] += 1
            team_stats[away_team]['D'] += 1
            team_stats[home_team]['Pts'] += 1
            team_stats[away_team]['Pts'] += 1
            team_stats[home_team]['Form'].append('D')
            team_stats[away_team]['Form'].append('D')

    # 2. Safely UPDATE the Group Teams sheet (without clearing it!)
    groups_sheet = get_google_sheet(GROUPS_SHEET_NAME)
    all_group_rows = groups_sheet.get_all_values()
    
    if not all_group_rows:
        print("⚠️ Group Teams sheet is empty.")
        return
        
    header = [h.strip() for h in all_group_rows[0]]
    if "Team" not in header:
        print("⚠️ Error: 'Team' column is missing from Group Teams sheet.")
        return

    col_map = {col: idx + 1 for idx, col in enumerate(header)}
    updates = []

    # Loop through each row in the spreadsheet and safely overwrite just the stat numbers
    for row_idx, row in enumerate(all_group_rows[1:], start=2):
        # Prevent index errors if row is partially empty
        while len(row) < len(header):
            row.append("")
            
        team_name = str(row[col_map["Team"] - 1]).strip()
        if not team_name:
            continue
            
        # Get calculated stats (or default to 0 if they haven't played yet)
        stats = team_stats.get(team_name, {'MP': 0, 'W': 0, 'D': 0, 'L': 0, 'GF': 0, 'GA': 0, 'Pts': 0, 'Form': []})
        
        # Prepare the cell updates for this row
        def queue_update(col_name, value):
            if col_name in col_map:
                updates.append(gspread.Cell(row_idx, col_map[col_name], str(value)))
                
        queue_update('MP', stats['MP'])
        queue_update('W', stats['W'])
        queue_update('D', stats['D'])
        queue_update('L', stats['L'])
        queue_update('Goals', f"{stats['GF']} - {stats['GA']}")
        queue_update('DIFF.', stats['GF'] - stats['GA'])
        queue_update('Pts', stats['Pts'])
        
        # Form displays last 5 matches (e.g., W, L, D)
        form_str = ", ".join(stats['Form'][-5:])
        queue_update('Form', form_str)

    if updates:
        print(f"🔄 Updating {len(updates)} cells in the Group Teams sheet safely in-place...")
        groups_sheet.update_cells(updates)
        print("✅ Group standings updated successfully!")
    else:
        print("⚠️ No updates were necessary.")

def calculate_and_update_top_scorers():
    """Fetches all match events to calculate and update a top scorers list."""
    print("\n➡️ Step 3: Calculating and updating top scorers...")
    
    headers = { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" }
    live_matches = []
    
    # Fetch a wide range of dates to ensure all tournament goals are captured
    for i in range(-50, 5):
        target_date = (datetime.now() + timedelta(days=i)).strftime("%Y%m%d")
        try:
            response = requests.get(f"{ESPN_API_URL}?dates={target_date}", headers=headers, timeout=15)
            response.raise_for_status()
            live_matches.extend(response.json().get('events', []))
        except Exception as e:
            print(f"⚠️ Could not fetch API data for top scorers on {target_date}: {e}")

    seen_ids = set()
    unique_matches = [match for match in live_matches if match.get('id') not in seen_ids and not seen_ids.add(match.get('id'))]

    player_goals = {}  # {'Player Name': {'goals': count, 'team': 'Team Name'}}
    TEAM_MAPPING = {
        "USA": "United States",
        "Korea Republic": "South Korea",
        "Bosnia and Herzegovina": "Bosnia-Herzegovina",
        "Bosnia-Herzegovina": "Bosnia-Herzegovina",
        "Czech Republic": "Czechia",
        "Turkey": "Türkiye",
        "Turkiye": "Türkiye",
        "DR Congo": "Congo DR",
        "Democratic Republic of the Congo": "Congo DR",
        "Democratic Republic of Congo": "Congo DR",
        "Ivory Coast": "Côte d'Ivoire"
    }

    for event in unique_matches:
        if event.get('status', {}).get('type', {}).get('state') == 'pre':
            continue

        try:
            competitors = event['competitions'][0]['competitors']
            home_competitor = next(c for c in competitors if c['homeAway'] == 'home')
            away_competitor = next(c for c in competitors if c['homeAway'] == 'away')
            home_team_id, away_team_id = home_competitor['team']['id'], away_competitor['team']['id']
            home_team_name = TEAM_MAPPING.get(home_competitor['team']['displayName'], home_competitor['team']['displayName'])
            away_team_name = TEAM_MAPPING.get(away_competitor['team']['displayName'], away_competitor['team']['displayName'])
        except (KeyError, IndexError, StopIteration):
            continue

        summary_data = {}
        event_id = event.get('id')
        if not event_id: continue

        base_api = ESPN_API_URL.split('?')[0].replace('/scoreboard', '')
        urls_to_try = [link.get('href', '').replace('http://', 'https://') for link in event.get('links', []) if 'api' in link.get('rel', []) and 'summary' in link.get('rel', [])]
        urls_to_try.extend([
            f"{base_api}/summary?event={event_id}",
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/all/summary?event={event_id}"
        ])

        for url in urls_to_try:
            try:
                summary_resp = requests.get(url, headers=headers, timeout=10)
                if summary_resp.status_code == 200:
                    summary_data = summary_resp.json()
                    break
            except Exception:
                pass

        key_events = []
        if summary_data:
            key_events = summary_data.get('keyEvents', [])
            if not key_events and 'plays' in summary_data:
                key_events = summary_data.get('plays', [])
            if not key_events and 'header' in summary_data:
                try:
                    key_events = summary_data['header']['competitions'][0].get('details', [])
                except (KeyError, IndexError):
                    pass
                    
        if not key_events:
            key_events = event['competitions'][0].get('details', [])

        for detail in key_events:
            detail_type = str(detail.get('type', {}).get('text') or '').lower()
            is_goal = detail.get('scoringPlay') == True
            
            if not is_goal:
                if 'goal' in detail_type or 'penalty' in detail_type:
                    is_goal = True
                    
            # Exclude own goals and shootout penalties from top scorers
            if 'own' in detail_type or 'shootout' in detail_type or 'miss' in detail_type or 'saved' in detail_type:
                is_goal = False
                
            if not is_goal: continue

            athlete_name = ""
            if 'participants' in detail and detail['participants']:
                athlete = detail['participants'][0].get('athlete', {})
                athlete_name = athlete.get('shortName', athlete.get('displayName', ''))
            elif 'athletesInvolved' in detail and detail['athletesInvolved']:
                athlete = detail['athletesInvolved'][0]
                athlete_name = athlete.get('shortName', athlete.get('displayName', ''))
            elif 'athlete' in detail:
                athlete = detail['athlete']
                athlete_name = athlete.get('shortName', athlete.get('displayName', ''))

            if not athlete_name: continue

            scoring_team_id = detail.get('team', {}).get('id')
            player_team_name = home_team_name if str(scoring_team_id) == str(home_team_id) else away_team_name

            if athlete_name not in player_goals:
                player_goals[athlete_name] = {'goals': 0, 'team': player_team_name}
            player_goals[athlete_name]['goals'] += 1
            player_goals[athlete_name]['team'] = player_team_name

    if not player_goals:
        print("👍 No goal scorer data to update.")
        return

    sorted_scorers = sorted(player_goals.items(), key=lambda item: item[1]['goals'], reverse=True)
    sheet_data = [['Rank', 'Player', 'Team', 'Goals']]
    sheet_data.extend([[i + 1, player, data['team'], data['goals']] for i, (player, data) in enumerate(sorted_scorers)])

    try:
        scorers_sheet = get_google_sheet(TOP_SCORERS_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        print(f"⚠️ Worksheet '{TOP_SCORERS_SHEET_NAME}' not found. Creating it...")
        spreadsheet = get_google_sheet(FIXTURES_SHEET_NAME).spreadsheet
        scorers_sheet = spreadsheet.add_worksheet(title=TOP_SCORERS_SHEET_NAME, rows=len(sheet_data) + 10, cols=4)

    print(f"🔄 Updating {len(sheet_data) - 1} players in the Top Scorers sheet...")
    scorers_sheet.clear()
    scorers_sheet.update(range_name='A1', values=sheet_data, value_input_option='USER_ENTERED')
    print("✅ Top Scorers sheet updated successfully!")

if __name__ == "__main__":
    update_live_scores()
    # Add a small delay to ensure sheet updates are processed before reading again
    time.sleep(2) 
    calculate_and_update_standings()
    calculate_and_update_top_scorers()
