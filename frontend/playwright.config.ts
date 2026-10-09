import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  use: { baseURL: 'http://127.0.0.1:18081', headless: true },
  webServer: {
    command: '../.venv/bin/python -m uvicorn tests.ui_server:app --host 127.0.0.1 --port 18081 --app-dir ..',
    url: 'http://127.0.0.1:18081/healthz',
    reuseExistingServer: false,
  },
})
