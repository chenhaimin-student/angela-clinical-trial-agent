# CDE Demo and Browser Worker

The Python demo accepts de-identified, fictional search profiles and calls a loopback Browser Worker or the local data layer.

The TypeScript worker isolates Chrome DevTools Protocol access behind a strict YAML configuration and a `127.0.0.1` HTTP service. Copy `cde-browser-worker/config/worker.example.yaml` to an ignored local file and set an absolute Chrome or Chromium path.

The repository does not include a browser binary, browser profile, downloaded page archive, screenshot, or live diagnostic response.

Browser Worker commands:

```bash
cd cde-demo/cde-browser-worker
pnpm install --frozen-lockfile
pnpm build
pnpm test
```

Live smoke scripts are optional developer tools and are not run by CI. Use them only against public information and never with a production profile or credentials.
