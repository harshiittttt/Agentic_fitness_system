# Landing / download page

Static, no build step. The page leads with the coaching-agent system; claims were checked against .claude/agents, fitness_os and mobile/src/screens, so re-check them if those change. Files: `index.html`, `style.css`, `config.js`, `main.js`.

## Publish the APK link
1. Build an APK with `eas build -p android` from `mobile/` (see docs/BUILD.md); copy the artifact URL.
2. Edit `config.js`: set `apkUrl` (and `version`, `supportEmail`). An empty `apkUrl` shows a disabled "Coming soon" button.
3. Redeploy the site.

## Free hosting (I could not verify any of these)
- GitHub Pages: Settings > Pages > Source "GitHub Actions". `.github/workflows/pages.yml` then deploys `site/` on push to main.
- Netlify: drag the `site/` folder onto app.netlify.com/drop, or set the publish directory to `site`.
- Cloudflare Pages: connect the repo, leave the build command empty, set the output directory to `site`.

The only external request is the Inter font from Google Fonts. Remove the three font `<link>` lines for zero external requests.

## Architecture diagrams (2026-11-27)
`index.html` now explains the whole project: what it does, the three architecture diagrams, one spoken turn with measured timings, the safety and memory design, the test results and what the first gym sessions found.
The diagrams are Mermaid, drawn in the browser from `<pre class="mermaid">` blocks (loaded from jsDelivr, pinned to mermaid 10.9.1). The same sources are in the repo `README.md`; if you change one, change the other. The text inside the `<pre>` blocks must stay HTML-escaped (`&lt;br/&gt;`), and each diagram is rendered one at a time with its own id (rendering them together makes them overwrite each other).
Without JavaScript or an internet connection the page shows the diagram source and a pointer to the README. Numbers on the page are the ones in the README results table (scripted and synthetic speech); update both together.
