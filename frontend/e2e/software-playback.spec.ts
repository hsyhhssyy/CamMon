import { test, expect } from './fixtures'

test.use({ hevcFixture: true, insecurePlayback: true })

test('HEVC and Opus fall back to streamed software playback with pause, seek, speed and cleanup', async ({ page }) => {
  test.setTimeout(60_000)
  const errors: string[] = []
  const assets: string[] = []
  const ranges: string[] = []
  const hevcRanges: string[] = []
  let hevcSource = ''
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error' && message.text().startsWith('[packages/')) console.log(message.text()) })
  page.on('request', request => {
    if (request.url().includes('/libmedia/')) assets.push(request.url())
    if (request.url().endsWith('/stream') && request.headers().range) ranges.push(request.headers().range)
    if (hevcSource && request.url().endsWith(hevcSource) && request.headers().range) hevcRanges.push(request.headers().range)
  })
  await page.goto('/')
  expect(await page.evaluate(() => window.isSecureContext)).toBeFalsy()
  await page.getByLabel('密码', { exact: true }).fill('browser-test-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('button', { name: '录像文件', exact: true }).click()
  // H.264 keeps the native player and does not load the software decoder.
  await page.getByRole('button', { name: '播放 day/00_20251008080000_20251008080100.mp4', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '在线播放', exact: true })
  await expect.poll(() => dialog.locator('video').evaluate(element => element.videoWidth)).toBeGreaterThan(0)
  expect(assets).toEqual([])
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()

  const hevc = page.getByRole('button', { name: '播放 day/97_20251008080000_20251008080100.mp4', exact: true })
  const download = await page.getByRole('article', { name: 'day/97_20251008080000_20251008080100.mp4', exact: true }).getByRole('link').getAttribute('href')
  hevcSource = download!.replace('/download', '/stream')
  await hevc.click()
  const player = dialog.getByLabel('兼容录像播放器', { exact: true })
  await expect(player).toHaveAttribute('data-ready', 'true', { timeout: 20_000 })
  await expect(dialog.locator('video')).toHaveCount(0)
  await expect(player.locator('canvas')).toBeVisible()
  await expect.poll(() => player.getAttribute('data-time').then(Number)).toBeGreaterThan(0)
  // Check pixels, not just a moving audio clock.
  const screenshot = await player.locator('canvas').screenshot()
  expect(screenshot.length).toBeGreaterThan(2000)
  expect(assets.some(url => /hevc.*\.wasm$/.test(url))).toBeTruthy()
  expect(assets.some(url => /opus.*\.wasm$/.test(url))).toBeTruthy()
  expect(assets.every(url => new URL(url).origin === new URL(page.url()).origin)).toBeTruthy()
  expect(ranges.some(range => /^bytes=\d+-\d+$/.test(range))).toBeTruthy()
  expect(hevcRanges.length).toBeGreaterThan(0)
  expect(hevcRanges.every(range => /^bytes=\d+-\d+$/.test(range))).toBeTruthy()
  for (const range of ranges.filter(range => /^bytes=\d+-\d+$/.test(range))) {
    const [start, end] = range.slice(6).split('-').map(Number)
    expect(end - start + 1).toBeLessThanOrEqual(1024 * 1024)
  }
  await dialog.getByRole('button', { name: '暂停录像', exact: true }).click()
  await expect(player).toHaveAttribute('data-paused', 'true')
  const pausedTime = Number(await player.getAttribute('data-time'))
  await page.waitForTimeout(350)
  expect(Math.abs(Number(await player.getAttribute('data-time')) - pausedTime)).toBeLessThan(0.2)
  await dialog.getByLabel('录像进度').fill('35')
  await expect.poll(() => player.getAttribute('data-time').then(Number)).toBeGreaterThanOrEqual(34)
  await dialog.getByLabel('播放速度').selectOption('2')
  await expect(player).toHaveAttribute('data-rate', '2')
  await dialog.getByLabel('录像音量').fill('0.5')
  await dialog.getByRole('button', { name: '播放录像', exact: true }).click()
  await expect.poll(() => player.getAttribute('data-time').then(Number)).toBeGreaterThan(35)
  await page.screenshot({ path: '/tmp/cammon-software-playback.png', fullPage: true })
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  // No new ranges should be requested after the detached player is destroyed.
  await page.waitForTimeout(200)
  const closedRanges = ranges.length
  await page.waitForTimeout(400)
  expect(ranges.length).toBe(closedRanges)
  await hevc.click()
  await expect(player).toHaveAttribute('data-ready', 'true', { timeout: 20_000 })
  await page.keyboard.press('Escape')
  expect(errors).toEqual([])
})

test('missing authentication never starts software decoding and closing cancels a pending fallback', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await page.getByLabel('密码', { exact: true }).fill('browser-test-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('button', { name: '录像文件', exact: true }).click()
  await page.route('**/api/files/*/stream', route => route.fulfill({ status: 401, body: '' }))
  await page.getByRole('button', { name: '播放 day/97_20251008080000_20251008080100.mp4', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '在线播放', exact: true })
  await expect(dialog.getByRole('alert')).toContainText('登录已过期')
  await expect(dialog.getByLabel('兼容录像播放器')).toHaveCount(0)
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()
  await page.unroute('**/api/files/*/stream')
  await page.route('**/libmedia/**/avplayer.js', async route => {
    await new Promise(resolve => setTimeout(resolve, 500))
    await route.continue().catch(() => {})
  })
  await page.getByRole('button', { name: '播放 day/97_20251008080000_20251008080100.mp4', exact: true }).click()
  await expect(dialog.getByRole('status')).toContainText('正在启用浏览器兼容播放')
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await page.waitForTimeout(800)
  await page.unroute('**/libmedia/**/avplayer.js')
  // Also cancel after the demuxer has started, while its decoder is loading.
  await page.route('**/libmedia/**/hevc*.wasm', async route => {
    await new Promise(resolve => setTimeout(resolve, 500))
    await route.continue().catch(() => {})
  })
  const decoderRequest = page.waitForRequest(request => /hevc.*\.wasm$/.test(request.url()))
  await page.getByRole('button', { name: '播放 day/97_20251008080000_20251008080100.mp4', exact: true }).click()
  await decoderRequest
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()
  await page.waitForTimeout(800)
  await page.getByRole('button', { name: '播放 day/00_20251008080000_20251008080100.mp4', exact: true }).click()
  await expect.poll(() => dialog.locator('video').evaluate(element => element.videoWidth)).toBeGreaterThan(0)
  await expect(dialog.getByRole('alert')).toHaveCount(0)
  expect(errors).toEqual([])
})
