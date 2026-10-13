import { test, expect } from '@playwright/test'
import type { ImportJob, ImportReport } from '../src/api'

test('import progress returns after navigation, reload and reopening a closed page', async ({ page, context }) => {
  const errors: string[] = []
  context.on('page', opened => opened.on('pageerror', error => errors.push(error.message)))
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await page.getByLabel('密码', { exact: true }).fill('browser-test-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '存储总览', exact: true })).toBeVisible()
  const devices = await (await page.request.get('/api/devices')).json()
  const report: ImportReport = {
    folder: '/data/旧录像', device_id: devices[0].id, recursive: true,
    total: 1, importable: 1, imported: 1, duplicates: 0, copies: 0, skipped: 0, failed: 0,
    bytes: 100, expired: 0,
    items: [{ path: '00_20251008080000_20251008080100.mp4', status: 'imported', size: 100, day: '2025-10-08', channel: '00' }],
  }
  const job: ImportJob = {
    id: 'background-import', device_id: devices[0].id, device_name: devices[0].name,
    folder: report.folder, recursive: true, status: 'running', created_at: 1791849600,
    finished_at: null, total_files: 2, current_path: 'nested/01_20251008080000_20251008080100.mp4', report, error: null,
  }
  let started = false
  let failNextPoll = false
  await context.route('**/api/import/jobs', async route => {
    if (failNextPoll) {
      failNextPoll = false
      await route.fulfill({ status: 503, json: { detail: '连接暂时中断' } })
    } else {
      await route.fulfill({ json: started ? [job] : [] })
    }
  })
  await context.route('**/api/import/preview', route => route.fulfill({ json: { ...report, total: 2, importable: 2, imported: 0 } }))
  await context.route('**/api/import', async route => {
    started = true
    await route.fulfill({ status: 202, json: job })
  })
  await page.getByRole('button', { name: '旧录像导入', exact: true }).click()
  await page.getByLabel('导入文件夹').fill(report.folder)
  await page.getByLabel('归属摄像机').selectOption(devices[0].id)
  await page.getByRole('button', { name: '预览导入', exact: true }).click()
  await page.getByRole('button', { name: '开始导入', exact: true }).click()
  await expect(page.getByRole('heading', { name: '导入进度', exact: true })).toBeVisible()
  await expect(page.getByRole('progressbar', { name: '导入进度' })).toHaveAttribute('value', '1')
  await expect(page.getByRole('progressbar', { name: '导入进度' })).toHaveAttribute('max', '2')
  await expect(page.getByText('已处理 1 / 2 个文件')).toBeVisible()
  await expect(page.getByText(job.current_path!, { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '预览导入', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '录像文件', exact: true }).click()
  await page.getByRole('button', { name: '旧录像导入', exact: true }).click()
  await expect(page.getByText('已处理 1 / 2 个文件')).toBeVisible()
  await page.reload()
  await page.getByRole('button', { name: '旧录像导入', exact: true }).click()
  await expect(page.getByText('已处理 1 / 2 个文件')).toBeVisible()
  await expect(page.getByLabel('导入文件夹')).toHaveValue(report.folder)
  await page.close()
  const reopened = await context.newPage()
  await reopened.goto('/')
  await reopened.getByRole('button', { name: '旧录像导入', exact: true }).click()
  await expect(reopened.getByText('已处理 1 / 2 个文件')).toBeVisible()
  await reopened.setViewportSize({ width: 390, height: 844 })
  expect(await reopened.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  await reopened.screenshot({ path: '/tmp/cammon-import-progress.png', fullPage: true })
  failNextPoll = true
  await expect(reopened.getByRole('alert')).toContainText('进度暂时无法刷新，将自动重试：连接暂时中断')
  await expect(reopened.getByText('已处理 1 / 2 个文件')).toBeVisible()
  await expect(reopened.getByRole('alert')).toHaveCount(0)
  job.status = 'completed'
  job.current_path = null
  job.finished_at = job.created_at + 10
  job.report = { ...report, total: 2, importable: 2, imported: 2, bytes: 200 }
  await expect(reopened.getByRole('heading', { name: '导入结果', exact: true })).toBeVisible()
  await expect(reopened.getByText(/共 2 个文件 · 已导入 2 个/)).toBeVisible()
  await expect(reopened.getByRole('button', { name: '预览导入', exact: true })).toBeEnabled()
  await reopened.reload()
  await reopened.getByRole('button', { name: '旧录像导入', exact: true }).click()
  await expect(reopened.getByRole('heading', { name: '导入结果', exact: true })).toBeVisible()
  await reopened.setViewportSize({ width: 390, height: 844 })
  expect(await reopened.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  expect(errors).toEqual([])
})
