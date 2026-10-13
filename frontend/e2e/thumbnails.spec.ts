import { test, expect } from '@playwright/test'
import type { ImportReport } from '../src/api'

test('import preview generates the current page sequentially and reuses previous page images', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('密码', { exact: true }).fill('browser-test-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '存储总览', exact: true })).toBeVisible()
  const devices = await (await page.request.get('/api/devices')).json()
  const items = Array.from({ length: 101 }, (_, index) => ({
    path: `${String(index).padStart(3, '0')}_20251008080000_20251008080100.mp4`,
    status: 'ready' as const, size: 100, day: '2025-10-08', channel: String(index),
    thumbnail_key: `thumbnail-${index}`, start: 1759881600, end: 1759881660,
  }))
  const report: ImportReport = {
    folder: '/data/旧录像', device_id: devices[0].id, recursive: true,
    total: 101, importable: 101, imported: 0, duplicates: 0, copies: 0, skipped: 0, failed: 0,
    bytes: 10100, expired: 0, items: items.slice(0, 25), preview_id: 'paged-preview', offset: 0, limit: 25,
  }
  await page.route('**/api/import/preview', route => route.fulfill({ json: report }))
  await page.route('**/api/import/previews/paged-preview?*', route => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset'))
    return route.fulfill({ json: { ...report, offset, items: items.slice(offset, offset + 25) } })
  })
  const requested: number[] = []
  let active = 0, maximum = 0
  let releaseHeld: () => void = () => {}
  const held = new Promise<void>(resolve => { releaseHeld = resolve })
  const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1sAAAAASUVORK5CYII=', 'base64')
  await page.route('**/api/import/previews/paged-preview/thumbnail?*', async route => {
    const index = Number(new URL(route.request().url()).searchParams.get('index'))
    requested.push(index); active++; maximum = Math.max(maximum, active)
    if (index === 50) await held
    await new Promise(resolve => setTimeout(resolve, 35))
    active--
    await route.fulfill({ contentType: 'image/png', body: pixel })
  })
  await page.getByRole('button', { name: '旧录像导入', exact: true }).click()
  await page.getByLabel('导入文件夹').fill(report.folder)
  await page.getByLabel('归属摄像机').selectOption(devices[0].id)
  await page.getByRole('button', { name: '预览导入', exact: true }).click()
  await expect(page.getByText('第 1–25 个，共 101 个 · 每页 25 个')).toBeVisible()
  const preview = page.getByRole('region', { name: '导入预览', exact: true })
  await expect.poll(() => requested.length).toBe(25)
  expect(requested).toEqual(Array.from({ length: 25 }, (_, index) => index))
  expect(maximum).toBe(1)
  await preview.getByRole('button', { name: '导入下一页' }).click()
  await expect(page.getByText('第 26–50 个，共 101 个 · 每页 25 个')).toBeVisible()
  await expect.poll(() => requested.length).toBe(50)
  expect(requested.slice(25)).toEqual(Array.from({ length: 25 }, (_, index) => index + 25))
  await preview.getByRole('button', { name: '导入上一页' }).click()
  await expect(page.getByText('第 1–25 个，共 101 个 · 每页 25 个')).toBeVisible()
  await preview.getByRole('article').first().scrollIntoViewIfNeeded()
  await expect(preview.getByRole('article').first().locator('img')).toBeVisible()
  await expect.poll(() => preview.getByRole('article').first().locator('img').evaluate(element => (element as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
  await expect(preview.getByRole('article').first().getByText('正在加载首帧…')).toHaveCount(0)
  expect(requested.length).toBe(50)
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  await page.screenshot({ path: '/tmp/cammon-thumbnail-import-mobile.png', fullPage: true })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.screenshot({ path: '/tmp/cammon-thumbnail-import.png', fullPage: true })
  // Leave a page while its first frame is still being generated.
  await preview.getByRole('button', { name: '导入下一页' }).click()
  await preview.getByRole('button', { name: '导入下一页' }).click()
  await expect.poll(() => requested.includes(50)).toBeTruthy()
  await preview.getByRole('button', { name: '导入下一页' }).click()
  await expect(page.getByText('第 76–100 个，共 101 个 · 每页 25 个')).toBeVisible()
  releaseHeld()
  await expect.poll(() => requested.includes(99)).toBeTruthy()
  expect(requested.filter(index => index >= 51 && index < 75)).toEqual([])
})
