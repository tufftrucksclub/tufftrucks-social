# Tuff Trucks Social

Self-hosted social media manager for tufftrucks.club: plans a week of posts with Claude, turns Shopify products
and Etsy reviews into graphics with new backdrops, writes captions per platform, and posts to Instagram,
Facebook, TikTok, Pinterest and X — auto-posting routine product spotlights and holding review posts for your OK.

**Start here → [SETUP.md](SETUP.md)**

```
run.py                 start the web app (http://localhost:8420)
app/agent.py           sync → plan → draft → render → approve → publish → measure
app/brain.py           Claude prompts (weekly plan, captions, review-photo rating, performance review)
app/studio/            graphics: backdrops, layouts, TikTok video
app/publishers/        Instagram, Facebook, TikTok, Pinterest, X
app/sources/           Shopify + Etsy
app/scheduler.py       background jobs
python -m app.tools    command-line helpers (check, sync, plan, meta-ids, etsy-shop-id…)
```
