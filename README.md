# GTA VI Feed

Tracks the most-watched GTA 6 videos on YouTube. GitHub Actions updates the list every 2 hours, and a page on GitHub Pages lets you browse it.

**Page tabs:** Hot (most views per hour over the past week), Today, This week, This month, All time, Newest, Rockstar (official videos only), and Saved. There's also a search box and a Hide Shorts switch.

**Output** (in the `docs/` folder):
- `index.html`: the web page
- `videos.json`: every tracked video with views, likes, length and upload date
- `feed.xml`: an RSS feed of the past two weeks' videos

## How it finds videos

1. **YouTube search (recommended).** If a `YOUTUBE_API_KEY` secret is set, each run searches all of YouTube for GTA 6 videos sorted by view count, over the past day, week, month and all time. It also refreshes the view counts for every video already on the list and removes videos that were deleted or taken down.
2. **Channel feeds (no setup).** Each run also reads the latest uploads from the channels in `config.json` and keeps the ones about GTA 6. On its own this only covers those channels.

Each run adds to the previous list, so videos stay in it after they drop out of search results. The list keeps every video from the past 30 days plus the 150 most-viewed of all time.

## Setup

1. **Allow the workflow to save changes.** Go to **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, and save.
2. **Add a YouTube API key** (free, about 5 minutes):
   1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and create a project (any name).
   2. Open **APIs & Services → Library**, search for **YouTube Data API v3**, and click **Enable**.
   3. Open **APIs & Services → Credentials → Create credentials → API key** and copy the key. Optionally click **Restrict key** and limit it to the YouTube Data API v3.
   4. In this repo, go to **Settings → Secrets and variables → Actions → New repository secret**. Name it `YOUTUBE_API_KEY` and paste the key.
3. **Run it once.** Open the **Actions** tab, pick **Update video feed**, and click **Run workflow**.
4. **Publish it.** Go to **Settings → Pages**, set Source to **Deploy from a branch**, branch `main`, folder `/docs`, and save. The page goes live at `https://gpcbl.github.io/gta6-vid/`.

## API quota

The free quota is 10,000 units a day. Each search costs 100 units, and looking up stats costs 1 unit per 50 videos. With 4 searches every 2 hours, the feed uses about 5,000 units a day. If you add search windows or run it more often, keep the total under 10,000. When the quota runs out, the run still finishes using the channel feeds.

## Customizing

Edit `config.json`:
- `search_query`: the YouTube search. `|` means OR.
- `search_windows`: which time ranges to search (`"days": null` means all time).
- `keywords`: a channel-feed video is kept only if its title or description contains one of these.
- `channels`: add a channel with its name and channel ID (the `UC…` code). To find it, open the channel on YouTube, tap **About → Share channel → Copy channel ID**. Mark official channels with `"official": true` so they appear under the Rockstar tab.

## Notes

- The page links to YouTube and uses YouTube's own thumbnails. It doesn't download or re-host any videos.
- The API key is stored as a GitHub secret. It never appears in the published files or the Actions log.
- Scheduled workflows pause after 60 days without repository activity. The feed commits keep it active, but if it stops, re-enable it from the Actions tab.

## Run it locally

```bash
YOUTUBE_API_KEY=your-key python fetch_videos.py   # or without the key for channel feeds only
```
