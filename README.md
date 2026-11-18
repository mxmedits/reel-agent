# Reel Agent: a hands-off Instagram page for GTA 6, Call of Duty and FC 27 clips

You add gameplay in the app. The agent does the rest, on GitHub's free computers, at no cost:

1. **Finds the best moments** in the clip (loud, busy action; menus and loading screens are pushed down).
2. **Watches each moment** with a free AI, rates it and skips weak ones, cutscenes and spoilers.
3. **Decides what kind of reel to make** and edits it: classic, slow motion, instant replay, freeze frame,
   meme caption, POV, top-3 countdown or montage, with a colour grade, sharpening, impact effects and captions.
4. **Writes the post**: three hook options scored against each other, a caption and five hashtags, shaped by
   what is working on Instagram this week.
5. **Asks for your OK**, then posts at the best times through Instagram's official system, answers comments, reads the stats.
6. **Keeps learning**: what top posts look like, how top reels are made, and which kinds of reel your own
   viewers watch.

**The app:** https://mxmedits.github.io/reel-agent/ (add it to your iPhone Home Screen).
**Alerts:** new reels, posts and problems are posted to the issue "Reel agent alerts", which GitHub sends you.

Built-in safety: nothing GTA 6 is posted before the **Nov 19, 2026** launch (Rockstar takes down pre-release
footage). Cutscenes and endings are skipped, and only Instagram's official API is used: no bots, no password.

## 📱 The app

| Tab | What it shows |
|---|---|
| Home | today's posting times, the **Add gameplay** button, what is up next |
| Reels | every reel waiting to post (watch it, edit the caption, post now, skip) and every posted reel with its numbers |
| Learned | what the agent read on Instagram this week, its trend research, and what works on your page |
| Settings | watermark, reels per day, posting times, how creative it gets, keys and the Instagram connection |

**Set it up once (about 20 minutes, all on the phone):**

1. Open the app link in Safari, tap Share, then **Add to Home Screen**. Open it from the new icon.
2. Tap **Create the key on GitHub**, choose the `reel-agent` repository, generate the key, paste it in the app.
3. In the app: **Add the free AI key** (Google Gemini, from aistudio.google.com/apikey).
4. In the app: **Connect Instagram** (the steps are on the screen; this is the long one).
5. Tap **Add gameplay** and pick a clip from Photos.

**How it works underneath.** The app uploads your clip to this repository in small parts. A GitHub Actions
workflow (`.github/workflows/agent.yml`) runs every 15 minutes and whenever the app asks: it edits new clips,
posts when a posting time has come, and saves its records to the `agent-data` branch, which the app reads.
Keys are stored as encrypted repository secrets; the app encrypts them on your phone before sending.

**Things to know:**
- This repository is public (that is what makes GitHub free here), so clips you add and the reels made from
  them can be seen by anyone who looks for the repository, before they are posted. Keys cannot be seen.
- GitHub starts scheduled runs a few minutes late at busy times, so a reel can go out up to about 20 minutes
  after its posting time.
- Clips up to about 1 GB upload fine on Wi-Fi. Keep the app open until the upload finishes.
- Voice commentary captions (Whisper) are not available in this mode.

## ✋ You approve every reel, and you can tell the agent what to change

Nothing goes to Instagram until you approve it (Settings, "Ask me before posting"; on by default).
New reels appear under **Waiting for your OK** in the app. Open one, watch it, then:
- **Approve**: it goes out at the next posting time. **Approve and post now** posts it right away. **Skip** drops it.
- **Tell the agent what to change**: write it in plain words ("the text is too small", "start earlier",
  "no shake", "brighter", "use a different moment", "make it a slow-motion replay"). The agent re-edits the reel
  from your original clip, replies with what it changed, and asks for your OK again.
  Say "always", "never" or "from now on" and the wish is saved for every future reel.
  With the free AI key the agent understands any wording; without it, it understands short notes like the examples.

The original clip is kept (as a release file) until all its reels are posted or skipped, so re-edits are possible.
The agent's reply only lists what it really changed in the new version, and says plainly what it couldn't do.

**Sound you can ask for:** remove the AI voiceover, mute or lower the game sound, add music, music from
Instagram, a different song, music louder or quieter.

## 🎵 Music on every reel

- **Music from Instagram** (Settings, on by default): once Instagram is connected, the agent picks a trending
  Instagram song that fits each reel while editing it, shows it to you before you approve, and Instagram adds
  it when the reel is posted. The app's preview can't play Instagram's music, so it plays without it.
- **A beat made for the reel**: when Instagram isn't connected or has nothing that fits, the agent makes its own
  royalty-free beat in the reel's mood (hype, intense, epic, chill, funny, suspense), with the build-up under
  the start and the drop on the big moment.
- **Your own tracks**: files in `music/<mood>/` (tracks you have the rights to) are used before the beats.

## 🪪 Your profile, kept follow-worthy

Instagram's API can read a profile but can't change one, for any app. So the agent reads yours every few hours,
scores it out of 100 (the ig-profile rubric), and writes the fixes: three options for the name field (the line
Instagram search matches), three bios, which reels to pin, and highlight names for later. The app shows them on
Home under **Fix my profile**, each with a **Copy** button. Paste one in Instagram's Edit profile and tap
**I changed it, check again**; the agent sees the change and scores it again.

What the agent does on its own: edits, captions, hashtags, posting at good times, replies to comments, reading
top posts and trends, learning from your numbers. What only you can do in the Instagram app, because Instagram
allows no app to: the profile fields above, pinning, highlights and following other accounts.

## 📺 Filmed your TV with your phone? That's handled

If a clip was filmed off a TV or monitor, the agent finds the screen, straightens it to a flat 16:9 picture,
crops the room away and steadies the hand shake. It then restores the picture (black level, colour cast,
camera noise) instead of adding film effects on top. iPhone HDR video is converted properly, so it doesn't
come out grey. Moments where the phone pointed away, the screen was black (loading, death screens) or the
phone was being moved are avoided when picking highlights. The sharpest reels still come from the console's
own recording (PS5: Create button, then the PlayStation App; Xbox: capture, then the Xbox app).

**No subtitles**: word-by-word subtitles are off. The title at the start, the question at the end and tags
like REPLAY or #1 stay. (`clips.subtitles` in the settings file turns subtitles back on.)

## 🎬 Kinds of reel, and how the agent chooses

| Kind | What it does | Good for |
|---|---|---|
| Classic | the payoff flashes first, then the build-up, punch zoom, big hook | any strong moment |
| Slow motion | the payoff at half speed | clean shots, goals, stunts |
| Instant replay | the moment at full speed, then a slow-motion replay with a REPLAY tag | clutches, goals |
| Freeze frame | freezes just before it happens, with a label, then plays out | fails, ambushes |
| Meme caption | white caption bar with a relatable line | funny moments |
| POV | a "POV: ..." line in a caption box | first-person moments |
| Top 3 countdown | three moments from one session, #3 to #1 | long recordings |
| Montage | four to six quick cuts of the best moments | long recordings |

Some reels also end on a question ("Rate this /10") on a dimmed last second, to pull comments.

For every clip the agent weighs: what the AI saw in it, what kinds of reel are common among top posts right
now, what has worked on your page, and variety (a kind used in the last few reels is marked down). About one
reel in five it tries a kind the page hasn't used lately, so it keeps learning. Change that under
Settings, "How creative the agent gets", or `clips.creativity` in the settings file.

## 🧠 What it learns, and from where

- **Instagram's top posts** (every two days, through your own connection): the hashtags on them, how their
  captions open, when they went live, and what kind of reel they are (POV, rate-this, top-N, clutch, fail...).
- **How top reels are made**: it watches a few of them and measures how long they run and how often they cut,
  and the AI describes their text and structure. Your reel length and montage pace follow that. The videos
  are only measured, never kept or reposted.
- **This week's top gaming Shorts** on YouTube (titles, views, lengths), if you add the free YouTube key.
- **Daily web research** on formats, sounds, timely moments per game and changes to how Instagram ranks reels.
- **Your own numbers** (daily): views of each kind of reel, hook formula, game and sound against your median.

If Meta has not opened hashtag search to your app, the agent says so in the Learned tab and relies on the
web research and YouTube data instead.

---

# Other ways to run it

The app above is the recommended way. These remain for anyone who wants their own server or computer.

## 💸 A free cloud server instead of GitHub

| Piece | Free option | Cost |
|---|---|---|
| Server | **Oracle Cloud Always Free**: an ARM server with 2 CPUs and 12 GB of memory | $0 (a card is needed only to verify your identity) |
| AI brain | **Google Gemini free tier** (`gemini-flash-lite-latest`) | $0, with daily limits |
| Trend data | **YouTube Data API** (this week's top gaming Shorts) + Instagram's trending sounds | $0 |
| Voiceover | Piper (built in) | $0 |
| Music | Instagram sounds (Audio API) | $0 |

You can switch any piece to a paid option later (Claude, a paid server) in Settings, without reinstalling.

**Free keys (5 minutes each, from your phone):**
- **Gemini:** aistudio.google.com → *Get API key* → paste it in Settings → *AI brain & keys*.
- **YouTube:** console.cloud.google.com → create a project → *APIs & Services* → enable **YouTube Data API v3**
  → *Credentials* → *Create credentials → API key* → paste it in Settings.

**Oracle free server:**
1. Sign up at **cloud.oracle.com** (Free Tier). Pick your home region carefully, because it can't be changed later.
2. *Compute → Instances → Create instance*:
   - Image: **Canonical Ubuntu 24.04**.
   - Shape: **Ampere VM.Standard.A1.Flex**, 2 OCPU / 12 GB. If you get "out of capacity", try 1 OCPU / 6 GB or another availability domain.
   - Keep "Assign a public IPv4 address" on.
   - Under *Show advanced options → Management*, paste the setup script from Reel Agent HQ (or `deploy/server-setup.sh` with your password).
3. Open the web ports. Go to *Networking → Virtual cloud networks → your VCN → Security Lists → Default → Add Ingress Rules*:
   source `0.0.0.0/0`, protocol TCP, destination port `80,443`. The script opens the server's own firewall.
4. Continue with step 3 below (open `http://YOUR-SERVER-IP`, upload the zip).

**Free-tier trade-offs, so nothing surprises you:**
- Gemini's free tier has daily limits that Google changes often, and Google may use free-tier requests to improve
  its products. On a busy day, once the limit is hit, captions fall back to templates until it resets at
  midnight Pacific.
- Gemini's free tier may not include web search. The trend brief then comes from the YouTube and Instagram data.
- Oracle sometimes has no free capacity in a region, and it can reclaim free servers that sit idle for long
  periods. If yours is ever reclaimed, create a new one with the same script and re-upload the zip.
- Captions from the free model are a bit less sharp than Claude's. Switch in Settings when the page earns.

## 📲 On the iPhone itself (a-Shell, for people comfortable typing commands)

Your iPhone does everything: share a clip, it edits the reels, writes the caption and hashtags, and posts.
It uses the free app **a-Shell** (Python + FFmpeg) and the built-in **Shortcuts** app. AI is free Gemini.

**Install (once)**

1. App Store → install **a-Shell** (free). Open it.
2. Type these lines, one at a time:
   ```
   cd ~/Documents
   curl -L -o ra.zip https://github.com/mxmedits/reel-agent/archive/refs/heads/main.zip
   unzip -o ra.zip
   mv reel-agent-main reel-agent
   cd reel-agent
   python3 -m reelagent phone-setup
   ```
   Setup installs what it needs, asks for your free Gemini key, connects Instagram and runs a speed test.

**Shortcut "Reel Agent" (share a clip → reel)**

Shortcuts app → + → name it **Reel Agent** → ⓘ → turn on **Show in Share Sheet**, accept **Media**. Add:
1. **Save File**: Shortcut Input → folder `a-Shell/reelagent-data/inbox`, "Ask Where to Save" off.
2. **Execute Command** (a-Shell), run **In App**:
   ```
   cd ~/Documents/reel-agent
   python3 -m reelagent phone
   ```
Now in Photos: open a gameplay clip → Share → **Reel Agent**. Keep a-Shell on screen while it edits.

**Automations (posting on time)** Shortcuts → Automation → + → run immediately, each with an
**Execute Command** (In App): `cd ~/Documents/reel-agent` then `python3 -m reelagent phone-tick`
- Time of Day, at each posting time (12:30, 18:00, 21:15)
- App → **Instagram** is opened (add "Open App: Instagram" as the last step)
- Charger is connected

An iPhone can't wake itself to post at an exact minute, so the agent catches up: whenever a tick runs
and a posting time has passed without its reel, it posts (inside `phone.active_hours`, with
`phone.min_gap_hours` between posts).

**Update later:** `cd ~/Documents/reel-agent` then `python3 -m reelagent update`. Your data lives in
`reelagent-data` and is never touched.

**Limits on the phone:** AI voiceover (Piper) and commentary subtitles (Whisper) need a computer or
server; on the phone reels use game sound, your music folder and Instagram's trending sounds.

## 🎨 Pro editing (the "Photoshop" pass)

Every reel gets: auto tone (levels, dark clips lifted), light denoise, a colour grade, sharpening, soft
glow, vignette, film grain, camera shake + flash + colour-split on the payoff, a flash on the teaser cut,
smoother slow motion, and a cover frame with the title on it. Looks: `vice` (GTA neon), `tactical` (COD
teal-orange), `stadium` (FC broadcast), `cinematic`, `night`, `clean`. The AI picks the look per clip from
the stills; without AI each game uses its own. Tune or switch off any part under `clips.fx` in config.

## 🔎 Reading Instagram itself (scout)

Every two days the agent reads Instagram's **Top posts** for each game's hashtags through your own
connection (Hashtag Search, max 30 hashtags a week, it uses 24) and learns:
- **trending hashtags**: ranked by how often they sit on top posts, weighted by engagement
- **caption patterns**: opening lines of the best posts, length, questions, CTAs, emoji
- **posting times**: when top posts went live; used until your own follower-activity data unlocks at 100 followers

These feed the caption writer, the hashtag picker and the scheduler. The daily trend brief also checks
for announced changes to how Instagram ranks Reels. Run it now: `python3 -m reelagent scout`.
Report: `output/reports/scout-DATE.md`. Add pages to learn from in `trends.benchmark_pages`.
If Meta has not enabled hashtag search for your app, the agent says so and uses web + YouTube data.

## 📱 Or run it on a free cloud server

The agent runs on a small rented server that's always on. You control everything from your phone's browser:
upload clips from your camera roll, add music, watch reels before they go out, post now/skip, settings, stats.

1. **Get a server** (Ubuntu 24.04, 2 GB+ RAM). Free: Oracle Cloud (see above). Paid:
   - **Hetzner Cloud**: cheapest. The smallest 4 GB server is roughly €4–6/month after their 2026 price changes.
   - **DigitalOcean**: easiest on a phone, with a Toronto region. The Basic 2 GB droplet is $12/month.
2. While creating it, open **Advanced → User data** (DigitalOcean) or **Cloud config** (Hetzner). Paste all of
   `deploy/server-setup.sh` there, after changing `PASSWORD="CHANGE_ME"` to your own password. Create the server.
3. Wait ~5 minutes, then open **http://YOUR-SERVER-IP** on your phone. It forwards you to a secure
   `https://…sslip.io/setup` page. Enter your password, pick `reel-agent.zip` and tap Install (3–5 min).
4. Open the dashboard (same address, without `/setup`), log in, then:
   **Settings** → add a free Gemini key (or a Claude key) and your @handle → **Connect Instagram** (steps are on the page).
5. Tap Share → **Add to Home Screen** so it opens like an app.

To share a clip, go to Home → **Add gameplay**, pick the game and choose videos. The agent takes it from there.
PS5/Xbox captures land on your phone through the PlayStation App / Xbox app; save them to your camera roll first.

To update the agent later, open `https://…sslip.io/setup` and upload the new zip. Settings, keys and reels are kept.

---

## 💻 Or run it on a Windows/Mac computer

## 1. Install (once, ~10 min)

You need a Windows or Mac computer with **Python 3.10+** ([python.org](https://www.python.org/downloads/)). On Windows, tick "Add Python to PATH".

- **Windows:** double-click `setup_windows.bat`
- **Mac:** double-click `setup_mac.command`

That installs FFmpeg + the Python packages and creates `config.yaml` and `.env`.

Open `.env` and paste your Claude API key (console.anthropic.com → API keys) after `ANTHROPIC_API_KEY=`.
Open `config.yaml` and set `handle: "@yourpage"`.

## 2. Connect Instagram (once, ~15 min)

The agent posts through Meta's official Instagram API, which needs a free developer app:

1. **Instagram app → Settings → Account type → switch to Creator** (or Business).
2. **Link it to a Facebook Page** you manage (Instagram → Accounts Center, or Page settings → Linked accounts). Create a Page if you don't have one.
3. Go to **developers.facebook.com → My Apps → Create App** and pick the **Business** type.
4. **Tools → Graph API Explorer**: select your app, then add these permissions:
   `instagram_basic, instagram_content_publish, instagram_manage_comments, instagram_manage_insights, pages_show_list, pages_read_engagement, business_management`
   → **Generate Access Token** → approve → copy the token.
5. Run:
   ```
   python -m reelagent setup-instagram
   ```
   Paste your App ID, App Secret (App settings → Basic) and the token. The agent swaps it for a **non-expiring** Page token and saves it to `.env`.

Your own account works with the app in development mode, so there's no app review.

Check everything:
```
python -m reelagent doctor
```

## 3. Turn on autopilot

- **Windows:** double-click `start_agent_windows.bat`. To start it with your PC, press Win+R, type `shell:startup` and put a shortcut to the file there.
- **Mac:** double-click `start_agent_mac.command`. To start it at login, add it in System Settings → General → Login Items.

Leave it running. Then drop recordings in:
```
inbox/gta6/   inbox/cod/   inbox/fc27/
```
Any length, any format (mp4/mkv/mov). OBS, NVIDIA ShadowPlay and console captures all work.
**Tip:** point `inbox:` in `config.yaml` at the folder where OBS/NVIDIA already saves recordings and you never have to move a file.

Each recording becomes up to 3 reels (`clips.per_video`), queued and posted one per slot.

## 4. Music and voiceover

**Music:** put tracks you have the rights to use into `music/<mood>/`. The moods are `hype`, `intense`, `epic`, `chill`, `funny` and `suspense`. Per-game folders like `music/gta6/hype/` take priority. Claude picks the mood per clip, and the agent lines the track's drop up with the payoff.

Use royalty-free or licensed music only (YouTube Audio Library, Pixabay Music, Epidemic Sound…). Instagram mutes or removes reels with unlicensed songs, and its in-app music library isn't available through the API.

**Voiceover:** by default it kicks in when a clip has none of your own commentary and no music for its mood. The free offline voice (Piper, downloaded on first use) needs no account. For more natural voices, set `voiceover.engine` to `elevenlabs` or `openai` and add that key to `.env`. With `disclose_ai_voice: true`, "AI voiceover" is added to the caption.

**Your own mic commentary:** run `pip install faster-whisper` and it gets transcribed into the captions automatically.

## 4b. How it keeps up with trends and the algorithm

**Instagram's ranking signals are built into every reel.** Instagram's head, Adam Mosseri, names watch time,
sends per reach (DM shares, the strongest signal for reaching non-followers) and likes per reach as the top signals.
The agent applies them like this:
- **Watch time:** a payoff-first opener, action in frame 1, the payoff before ~60% of the clip, an ending that loops.
- **Sends:** captions favour "send this to your duo who..." calls to action.
- **Likes and comments:** "rate it /10" style prompts.
- **Search:** the game and the moment named in the first caption line.
- **Hashtags:** 5 at most.

**Daily trend research.** Once a day Claude searches the web for what's working for gaming Reels this week. It looks
for formats, edit styles, music styles, timely moments per game (launches, seasons, patches) and the hashtags in use.
It writes a trend brief (Stats → `trends-DATE`) that feeds every hook, caption, edit-style choice and sound search.
It needs web search turned on for your API key in the Anthropic Console (console.anthropic.com). Each search
adds a small extra cost.

**Edit styles.** Claude picks one per clip using the trend brief:
- `classic`: payoff teaser, punch zoom and big hook text.
- `slowmo`: half-speed payoff, smooth on 60 fps recordings.
- `meme_bar`: a white caption bar with a relatable line, for fails and POV moments.

The daily audit measures which style beats your median and feeds that back in.

**Trending Instagram sounds.** When a reel posts, the agent pulls what's trending from Instagram's Audio API. It also
searches the trend brief's music terms and asks Claude to pick a sound that fits the clip (or none). The sound is
attached with the game audio kept underneath. Only sounds Instagram clears for third-party use are offered, so the
list can differ from the app's, and the sound always starts from its beginning. If Instagram refuses, the reel posts
with its own audio. Your own music library (section 4) is used when this is turned off.

**Trial reels (smart).** Reels the agent is less sure about (quality under 8) go out as trial reels. Non-followers see
them first, and Instagram shares them to your followers automatically if they perform. If your account isn't enabled
for trial reels yet, the agent posts normally and tries again a week later.

**Smart posting times.** From 100 followers on, Instagram shares when your followers are online. Each week the agent
moves the posting slots to just before your followers' peak hours, always between 10:00 and 23:30 your time.

All of this can be switched off under Settings → **Reach & trends**.

## 5. Settings worth knowing (`config.yaml`)

| setting | default | what it does |
|---|---|---|
| `schedule.post_times` | 12:30, 18:00, 21:15 | one reel per slot. A slot missed by more than 2 h (PC asleep) is skipped, not crammed in |
| `clips.length` | 24 | seconds per reel |
| `review.require_approval` | false | set to true to approve each reel (`python -m reelagent approve all`) |
| `review.min_quality` | 6 | Claude's 1–10 bar to be posted |
| `games.*.layout` | blur / zoom | `blur` = full frame on a blurred background, `zoom` = centre crop for shooters, `crop` = full-height crop |
| `engagement.reply_to_comments` | true | auto-replies (max 15 per run, 60/day) |
| `voiceover.mode` | auto | auto / always / off |
| `clips.style` | auto | auto (Claude picks with the trend brief) / classic / slowmo / meme_bar |
| `instagram.trending_audio` | true | attach a trending Instagram sound that fits |
| `instagram.trial_reels` | smart | smart / always / off |
| `schedule.auto_times` | true | follow your followers' online hours (100+ followers) |
| `trends.enabled` | true | daily trend research |

Adding a game: copy a block under `games:` with its own `match` words, hashtags, hooks and `music_mood`, then make `inbox/<name>/`.

## 6. Commands

```
python -m reelagent serve            # phone dashboard + autopilot (what the server runs)
python -m reelagent watch            # autopilot only (what the start scripts run)
python -m reelagent watch --dry-run  # everything except posting
python -m reelagent doctor           # health check
python -m reelagent queue            # every reel + status + link
python -m reelagent post-now         # post the next reel right now
python -m reelagent rehook cod-0004 "NEW HOOK TEXT"
python -m reelagent replies | audit | profile
```
Finished reels live in `output/clips/<id>/` (reel.mp4, cover.jpg, caption, meta.json). Reports go to `output/reports/` and the log to `output/state/agent.log`.

## 7. The instagram-agent-skill pack inside

`vendor/instagram-agent-skill/` is Jake Schincariol's MIT-licensed pack ([repo](https://github.com/Jakeschincariol/instagram-agent-skill)), pinned at commit `d03c56b`. This is how the agent uses each part:

| skill | used for |
|---|---|
| ig-reel (hookscore.py, hooks.json, beats.py) | 3 hooks per reel from the 26 formulas, scored, best one goes on screen; voiceover scripts timed |
| ig-human (humanize.py, detect.py) | every caption and reply cleaned of AI tells and scored |
| ig-caption (caption.py) | caption linted: 125-char feed window, game name, one ask, 5 hashtags max; one auto-rewrite if it fails |
| ig-reply | comment triage and replies; abusive comments hidden, noise ignored |
| ig-audit + ig-viral (swipe.py) | daily stats → outlier multiple vs your median, sends/reach, formula naming → `learnings.json` |
| ig-profile (rubric.json) | 100-point profile score, name field and bio rewrites to paste from the app (the API can't edit profiles); re-scored when the profile changes |
| ig-plan | "next up" schedule section in each audit |
| ig-repurpose | that's the core loop: one long recording → several standalone reels |
| ig-story, ig-carousel, ig-dm, ig-comment | playbooks included; **not automated**. Carousels need publicly hosted images, DMs need Meta's Messaging API review, and commenting on other accounts isn't possible through the official API (bots that do it get accounts blocked) |

## 8. Honest limits

- **Instagram only for now.** YouTube Shorts can be added later. Note that Google locks API uploads to private until your project passes their free audit.
- **Trend research reads the public web.** It sees articles and announcements, not Instagram's private trend data,
  and Instagram's in-app "trending" sound list isn't fully available to outside apps.
- **"Viral" isn't guaranteed.** What the agent controls (hooks, edit, timing, replies, learning from your own numbers) gives you the best odds; the clips themselves still decide.
- **The agent runs on your server or computer.** If that machine is off, nothing posts; a slot missed by more than 2 h is skipped.
- **Rockstar's video policy** welcomes gameplay clips but is written for non-commercial fan use, and pre-release footage, endings and cutscene compilations get taken down. The agent enforces the last three automatically.
- **Meta API rate limits** apply. `doctor` shows your publishing quota.
