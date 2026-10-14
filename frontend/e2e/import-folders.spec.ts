import { test, expect } from './fixtures'
import type { Page } from '@playwright/test'
import type { ImportFolders, PathOwnership } from '../src/api'

const ownership: PathOwnership = {
  root: '/data', path: '/data', exists: true, kind: 'directory', status: 'unassigned',
  owners: [], bindings: [], recording_ids: [],
}
const listing: ImportFolders = {
  root: '/data', path: '/data', parent: null, ownership,
  folders: [{ name: '旧录像', path: '/data/旧录像', ownership: { ...ownership, path: '/data/旧录像' } }],
}

async function openImport(page: Page) {
  await page.goto('/')
  await page.getByLabel('密码', { exact: true }).fill('browser-test-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('button', { name: '旧录像导入', exact: true }).click()
}

test('folder dialog opens immediately during a slow request and selects a child', async ({ page }) => {
  let release!: () => void
  const responseReady = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/import/folders*', async route => {
    const path = new URL(route.request().url()).searchParams.get('path')
    if (!path) await responseReady
    await route.fulfill({ json: path ? {
      ...listing, path, parent: '/data', ownership: { ...ownership, path }, folders: [],
    } : listing })
  })
  await openImport(page)
  await page.getByRole('button', { name: '浏览文件夹', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '浏览文件夹', exact: true })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('status')).toHaveText('正在加载文件夹和摄像机归属…')
  await expect(dialog.getByRole('button', { name: '选择此文件夹', exact: true })).toBeDisabled()
  release()
  await expect(dialog.getByRole('button', { name: '旧录像 尚未归属摄像机', exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '选择此文件夹', exact: true })).toBeDisabled()
  await dialog.getByRole('button', { name: '旧录像 尚未归属摄像机', exact: true }).click()
  await expect(dialog.getByRole('region', { name: '文件归属查询' }).getByRole('status')).toContainText('/data/旧录像')
  await dialog.getByRole('button', { name: '选择此文件夹', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page.getByLabel('导入文件夹')).toHaveValue('/data/旧录像')
})

test('failed folder request stays visible and can be retried', async ({ page }) => {
  let requests = 0
  await page.route('**/api/import/folders*', route => ++requests === 1
    ? route.fulfill({ status: 503, contentType: 'text/html', body: '<h1>Service unavailable</h1>' })
    : route.fulfill({ json: listing }))
  await openImport(page)
  await page.getByRole('button', { name: '浏览文件夹', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '浏览文件夹', exact: true })
  await expect(dialog.getByRole('alert')).toContainText('HTTP 503')
  await expect(dialog.getByRole('button', { name: '选择此文件夹', exact: true })).toBeDisabled()
  await dialog.getByRole('button', { name: '重试', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '旧录像 尚未归属摄像机', exact: true })).toBeVisible()
  await expect(dialog.getByRole('alert')).toHaveCount(0)
})

test('closing a loading folder dialog cancels it and a later response does not reopen it', async ({ page }) => {
  let release!: () => void
  const responseReady = new Promise<void>(resolve => { release = resolve })
  let requests = 0
  await page.route('**/api/import/folders*', async route => {
    if (++requests === 1) await responseReady
    await route.fulfill({ json: listing })
  })
  await openImport(page)
  await page.getByRole('button', { name: '浏览文件夹', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '浏览文件夹', exact: true })
  await expect(dialog.getByRole('status')).toHaveText('正在加载文件夹和摄像机归属…')
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  release()
  await page.getByRole('button', { name: '浏览文件夹', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '旧录像 尚未归属摄像机', exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
})

test('folder request timeout shows a retry action instead of staying busy', async ({ page }) => {
  let release!: () => void
  const responseReady = new Promise<void>(resolve => { release = resolve })
  let requests = 0
  await page.route('**/api/import/folders*', async route => {
    if (++requests === 1) await responseReady
    await route.fulfill({ json: listing })
  })
  await openImport(page)
  await page.clock.install()
  await page.getByRole('button', { name: '浏览文件夹', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '浏览文件夹', exact: true })
  await expect(dialog.getByRole('status')).toHaveText('正在加载文件夹和摄像机归属…')
  await page.clock.fastForward(20001)
  await expect(dialog.getByRole('alert')).toHaveText('加载文件夹超时，请重试。')
  release()
  await dialog.getByRole('button', { name: '重试', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '旧录像 尚未归属摄像机', exact: true })).toBeVisible()
})

test('an invalid initial folder can return to the storage root', async ({ page }) => {
  await page.route('**/api/import/folders*', route => new URL(route.request().url()).searchParams.has('path')
    ? route.fulfill({ status: 404, json: { detail: '导入文件夹不存在或不是目录' } })
    : route.fulfill({ json: listing }))
  await openImport(page)
  await page.getByLabel('导入文件夹').fill('/data/missing')
  await page.getByRole('button', { name: '浏览文件夹', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '浏览文件夹', exact: true })
  await expect(dialog.getByRole('alert')).toHaveText('导入文件夹不存在或不是目录')
  await dialog.getByRole('button', { name: '存储根目录', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '旧录像 尚未归属摄像机', exact: true })).toBeVisible()
})
