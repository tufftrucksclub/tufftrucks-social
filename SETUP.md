# Tuff Trucks Social — setup guide

A self-hosted social media manager for tufftrucks.club. It runs on your home PC or a
Raspberry Pi (same idea as the order desk) and you use it from any browser on your network.

**What it does every week, on its own**

1. Syncs your Shopify products, Etsy listings and Etsy reviews (every 6 hours).
2. Has Claude rate every new review photo (is it sharp, does it show the product, is a plate or face visible?).
3. Sunday afternoon: Claude reviews last month's results, then plans next week — which products, which reviews,
   which backdrop, which day — and writes separate copy for Instagram, Facebook, TikTok, Pinterest and X.
4. Renders every graphic in each platform's size, plus a 6–20 second video from the post's photos for TikTok and
   Instagram / Facebook Reels (see **Videos** below).
5. Product spotlights auto-approve. Review posts, promos, and anything Claude isn't sure about wait for your OK.
6. Publishes on schedule, then pulls likes/comments/shares/saves so next week's plan leans toward what worked.

Safety rails: it starts in **dry-run** (does everything except publish), there's a **Pause all** button,
and auto-approved posts can't go out until a **hold window** (default 12 h) has passed — so you always get a look.

---

## 1. Install

**Raspberry Pi / Linux / Mac**
```bash
cd tufftrucks-social
bash install.sh          # installs ffmpeg + Python packages into .venv, creates .env
.venv/bin/python run.py
```

**Windows**
Install Python 3.11+ from python.org (tick "Add to PATH") and ffmpeg (`winget install Gyan.FFmpeg`),
then double-click **start.bat**. The first run takes a few minutes; after that it starts right up.

Open **http://localhost:8420** (or `http://<that-computer's-IP>:8420` from your phone or another PC).

> **Try it first:** set `DEMO_MODE=1` in `.env`, start it, and hit **Plan next week**. You'll get sample
> products, reviews and posts to click through. Before connecting real accounts, set `DEMO_MODE=0` and
> delete the `data` folder so the samples go away.

Check what's connected any time with: `python -m app.tools check`

---

## 2. Claude (the brains)

1. Go to **console.anthropic.com** → API keys → create a key.
2. Put it in `.env` as `ANTHROPIC_API_KEY=...`.
3. Test: `python -m app.tools test-claude`

API usage is billed separately from a claude.ai subscription, by the amount of text processed. At this
volume (one weekly plan, ~5–7 posts of captions, a handful of photo ratings) it's small — check the current
per-model pricing on Anthropic's site. `CLAUDE_MODEL` in `.env` picks the model.

Without a key the app still runs, using a simple rotation planner and template captions.

---

## 3. Shopify (products + hosts the post images)

Instagram, Facebook and Pinterest download each image from a public link. Rather than opening your home
network to the internet, the app uploads finished graphics to your **Shopify Files** and hands the platforms
that link.

Since Jan 2026 new custom apps are made in Shopify's **Dev Dashboard**:

1. Go to **dev.shopify.com/dashboard** → create an app.
2. **Versions → Configure** Admin API scopes: `read_products`, `read_files`, `write_files`, `read_orders` → release.
3. Install the app on your store from the app's Home tab.
4. Copy **Client ID** and **Client secret** from Settings into `.env` with `SHOPIFY_STORE=<your>.myshopify.com`.

The app trades those for a 24-hour token on its own and refreshes it. (If you still have an old
`shpat_…` token from an admin-created custom app — the order desk may use one — `SHOPIFY_ACCESS_TOKEN` works too.)

Test on **Settings → Shopify → Test upload**.

---

## 4. Etsy (reviews + listings)

1. **etsy.com/developers** → Create a new app (personal/seller tool). You get a **keystring** and **shared secret**.
2. Put both in `.env`.
3. Find your shop ID: `python -m app.tools etsy-shop-id YourEtsyShopName` → put it in `ETSY_SHOP_ID`.

Read-only; no Etsy login needed. Etsy's API doesn't give buyer names, and only includes the buyer's photo when
their privacy settings allow it.

### 4b. iCloud Photos (optional — your own photos)

Open the **Photos** page → enter your Apple ID and password → type the 6-digit code Apple sends to your iPhone.
The password is used once and not saved; the login is kept in `data/icloud/` and lasts about two months, after
which the Photos page asks you to log in again. On a Pi with no browser, run `python -m app.tools icloud-login`.

- **Advanced Data Protection** on? Turn on *Access iCloud Data on the Web* first (iPhone → Settings → your name → iCloud).
- Best results: make a **“Tuff Trucks” album** on your phone, drop good shots in, and put that album name on the
  Photos page — then only those get scanned. Otherwise it looks at the last 90 days of the whole library.
- Have **iCloud for Windows** syncing photos? Its folder is scanned automatically too; add any other folders on the
  Photos page.

Claude rates each new photo (about 1–2¢ each, six per call) and flags plates, faces and dull backgrounds.

---

## 5. Facebook + Instagram

Requirements: Instagram must be a **Business or Creator** account, linked to your **Facebook Page**.

1. **developers.facebook.com** → My Apps → Create app → type **Business**. Add the **Instagram** and
   **Facebook Login for Business** products.
2. **business.facebook.com → Settings → Users → System users** → add one (Admin) → **Add assets**: your Page
   (full control) and your Instagram account.
3. **Generate token** for that system user, choose your app, set it to never expire, and tick:
   `pages_show_list, pages_read_engagement, pages_manage_posts, instagram_basic, instagram_content_publish,
   read_insights, instagram_manage_insights, business_management`.
4. Paste the token on **Settings → Facebook + Instagram** (or `META_ACCESS_TOKEN` in `.env`).
5. Get the IDs: `python -m app.tools meta-ids` → paste the Page ID and Instagram user ID → **Test Meta connection**.

Since the app only posts to accounts you own and admin, it should work while your Meta app stays in
development mode; Meta's app review is for apps other people log into.

---

## 6. TikTok

1. **developers.tiktok.com** → Manage apps → create an app. Add **Login Kit** and **Content Posting API**
   (turn on upload/"Direct Post" as offered).
2. Scopes: `user.info.basic`, `video.upload`, `video.list` (and `video.publish` if you'll ever use direct posting).
3. Redirect URI: `http://localhost:8420/oauth/tiktok/callback` (see *Connecting* below if it insists on https).
4. Put `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` in `.env` → **Settings → TikTok → Connect**.

**How it posts:** in the default **inbox** mode, each TikTok post arrives in your TikTok app as a draft
(notification in your inbox). Add a trending sound, paste the caption (copy button on the dashboard), tap Post,
then hit **Posted ✓**. TikTok only lets apps post publicly after it audits the app — before that, direct posts
are forced to private. If you later pass the audit, switch Settings → TikTok to **Post directly** and reconnect.

---

## 7. Pinterest

1. **developers.pinterest.com** → My apps → Connect app. Redirect URI: `http://localhost:8420/oauth/pinterest/callback`.
2. `PINTEREST_APP_ID` / `PINTEREST_APP_SECRET` in `.env` → **Settings → Connect Pinterest** → pick a board.
3. New apps get **Trial access**: pins you create are only visible to you. To go public, request **Standard
   access** in the developer portal. Pinterest wants a screen recording showing the full **Connect** login flow
   and a pin being created from the app — do one with dry-run **off** and Pinterest as the only platform on a test post.

---

## 8. X

1. **developer.x.com** → create a Project + App → **User authentication settings**: OAuth 2.0, type
   **Web App**, permissions **Read and write**, callback `http://127.0.0.1:8420/oauth/x/callback`
   (X doesn't accept `localhost`; Settings shows the exact URL to paste). Website URL: `https://tufftrucks.club`.
2. Copy the OAuth 2.0 **Client ID** and **Client Secret** into `.env` → **Settings → Connect X**.
3. Add credits in the X developer console (pay-per-use). Roughly 1.5¢ per post, ~20¢ if the post contains a
   link — so links are off for X by default (Settings → Platforms to change). Stats reads cost about half a cent
   each, so the app only checks X stats twice per post.

---

## Connecting (the OAuth logins)

The **Connect** buttons send you to TikTok / Pinterest / X to approve, then back to
`PUBLIC_BASE_URL/oauth/<platform>/callback`. Do the connecting from a browser **on the same computer** as the app
so `localhost` works. If a platform refuses an `http://localhost` redirect, open a temporary https tunnel just
for the login (for example `cloudflared tunnel --url http://localhost:8420`), set `PUBLIC_BASE_URL` to the https
address it prints, register that callback, restart, connect, then put `PUBLIC_BASE_URL` back. Tokens refresh
themselves after that.

---

## 9. Go live

1. Connect what you want; untick platforms you're not ready for (**Settings → Platforms**).
2. Do a dry run: **Plan next week**, open a post, **Publish now** — every step runs except the real post.
3. **Settings → Safety → untick Dry run.**
4. First live week, consider unticking *Product spotlight* under "Can publish without me" so you approve
   everything; turn it back on once you trust the captions.

## 10. Keep it running

**Raspberry Pi / Linux:** edit paths/user in `deploy/tufftrucks-social.service`, then
```bash
sudo cp deploy/tufftrucks-social.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tufftrucks-social
```
**Windows:** run `start.bat` once to set up `.venv`, then run `C:\GitRepos\Add Tuff Trucks apps to Startup.vbs`. It makes a
scheduled task, "Tuff Trucks Social", that starts it in the background when you sign in. `start-social-hidden.vbs` also starts
it through that task, so it never runs inside another program, such as the Claude app, which would close it when that
program closes or updates. Stop it with `stop-social.vbs`.

The computer needs to be on for posts to go out. If it's off at posting time, the post goes out when it's back
(the dashboard shows anything that failed, with a Retry button).

**Raspberry Pi tip:** background removal and video rendering are slow on a Pi (a video takes 15–60 s on a PC,
several minutes on a Pi). Set `REMBG_MODEL=u2netp` in `.env`
for faster cut-outs; planning runs in the background either way.

---

## Everyday use

- **Dashboard** — what needs your OK, what's coming up, TikTok drafts to finish, anything that failed.
- **Post page** — swap the backdrop or headline (graphics re-render), edit each platform's caption, change the
  time, approve. Editing an auto-approved post puts it back in your queue.
- **Reviews** — every Etsy review with Claude's photo score. *Photo + backdrop post* puts the buyer's picture on a
  new backdrop with their quote; *Quote card* makes a text graphic. A ⚑ means a plate/face might be visible.
- **Photos** — your iCloud / folder photos, best first, with Claude's score and what's in each one. *Make a post*
  turns one into a truck showcase (always waits for your OK). *New background…* swaps a parking lot or garage for
  something better and saves it as a new photo:
  - **Backdrop** (free, instant): the truck is cut out and set on any Studio backdrop, including scenery photos you
    upload as backdrops.
  - **AI scene** (needs `OPENAI_API_KEY`, ~20¢): a realistic scene is painted around the truck, matched to its light
    and angle. The truck's own pixels are pasted back on top, so your products are never altered.
  Photos with a dull background and a clean subject are cut out onto a backdrop automatically when they become posts.
- **Studio** — any product, review or library photo on any backdrop, every size at once. Placement *Cut out*
  replaces the photo's background with the backdrop; *Full photo* fills the frame. Upload your own backdrops
  (shop, trail, truck meet) and they show up as styles.
- **Videos** — every post gets a short vertical video made from its photos: each photo zooms and pans, the
  headline pops in as the hook, the product name or review quote animates in, and it ends on a "Link in bio" card
  with your web address. TikTok always gets the video; Instagram and Facebook get it as a **Reel** instead of the
  image (Settings → Video; each post can override it). Three styles: *Quick cuts* (fast, punchy — product
  spotlights), *Showcase* (slow, smooth — builds and behind the scenes) and *Review* (the customer's quote types
  in over their photo). Claude picks one when it plans; change it on the post page, where you can also pick which
  photos go in and in what order. Product listing photos on a white background are cut out onto the backdrop.
  **Music:** drop royalty-free tracks (MP3 / M4A / WAV) into `data/music` and each video gets one (or choose one per
  post). Only use music you have the rights to — YouTube Audio Library and Pixabay Music are free. TikTok's trending
  sounds can't be added by apps, but in inbox mode you can add one in the TikTok app before you post.
- **Insights** — numbers by platform and post type; **Ask Claude what's working** writes a plain-English review
  that next week's planner reads.

Customer photos: reviewers own their photos. Featuring them is common practice, and the app never names buyers,
but a quick "mind if we share your pic?" message on Etsy is the safe, friendly move for anything you boost.

## Troubleshooting

| Problem | Fix |
|---|---|
| Instagram: "media URL" / "could not fetch" | Run Settings → Shopify → Test upload; the image must be on a public link. |
| Reel fails / "video isn't rendered" | Open the post and **Save changes** to re-render the video; check the activity log for ffmpeg errors. |
| Facebook: permissions error | The system-user token is missing `pages_manage_posts` or the Page asset. Regenerate it. |
| TikTok draft never arrives | Update the TikTok app (inbox drafts need a recent version); check you approved `video.upload`. |
| Pinterest pins don't show up publicly | Still on Trial access — request Standard access. |
| X: 402 / credits error | Add credits in the X developer console. |
| Nothing posts | Dashboard banner: is it paused or in dry run? Is the post *Approved*? Is the computer awake? |

Files: everything lives in `data/` (database, rendered graphics, cached photos). Back that folder up and you've
backed up the whole thing.
