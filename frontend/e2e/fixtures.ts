import { test as base, expect } from '@playwright/test'
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { networkInterfaces, tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

// Storage settings and import jobs mutate the gateway, so each test owns a server.
export const test = base.extend<{ hevcFixture: boolean; insecurePlayback: boolean }>({
  hevcFixture: [false, { option: true }],
  insecurePlayback: [false, { option: true }],
  baseURL: async ({ hevcFixture, insecurePlayback }, use) => {
    const directory = mkdtempSync(join(tmpdir(), 'cammon-browser-'))
    const server = spawn(resolve('../.venv/bin/python'), [
      '-m', 'uvicorn', 'tests.ui_server:app', '--host', insecurePlayback ? '0.0.0.0' : '127.0.0.1', '--port', '0', '--app-dir', '..',
    ], { env: { ...process.env, CAMMON_BROWSER_TEST_DIR: directory, CAMMON_BROWSER_HEVC: hevcFixture ? '1' : '0' }, stdio: ['ignore', 'pipe', 'pipe'] })
    const closed = new Promise<void>(done => server.once('close', () => done()))
    try {
      const url = await new Promise<string>((done, fail) => {
        let output = ''
        const timeout = setTimeout(() => fail(new Error(`Browser test server timed out:\n${output}`)), 60_000)
        const collect = (data: Buffer) => {
          output += data.toString()
          const match = output.match(/Uvicorn running on (http:\/\/(?:127\.0\.0\.1|0\.0\.0\.0):\d+)/)
          if (match) { clearTimeout(timeout); done(match[1]) }
        }
        server.stdout.on('data', collect)
        server.stderr.on('data', collect)
        server.once('error', error => { clearTimeout(timeout); fail(error) })
        server.once('exit', code => {
          clearTimeout(timeout)
          fail(new Error(`Browser test server exited (${code}):\n${output}`))
        })
      })
      const address = Object.values(networkInterfaces()).flat().find(item => item?.family === 'IPv4' && !item.internal)?.address
      if (insecurePlayback && !address) throw new Error('An IPv4 interface is required for insecure HTTP playback tests')
      await use(insecurePlayback ? url.replace('0.0.0.0', address!) : url)
    } finally {
      server.kill('SIGTERM')
      const timeout = setTimeout(() => server.kill('SIGKILL'), 5000)
      await closed
      clearTimeout(timeout)
      rmSync(directory, { recursive: true, force: true })
    }
  },
})

export { expect }
