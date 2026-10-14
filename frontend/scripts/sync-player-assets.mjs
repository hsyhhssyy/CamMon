import { cp, mkdir, readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

// Preserve libmedia's own chunk URLs and worker modules instead of rebundling
// its webpack runtime. All decoder assets are served from this application.
const source = new URL('../node_modules/@libmedia/avplayer/', import.meta.url)
const { version } = JSON.parse(await readFile(new URL('package.json', source), 'utf8'))
if (version !== '1.3.1') throw new Error('Update the vendored Wasm modules when changing the player version')
const destination = new URL(`../public/libmedia/${version}/js/`, import.meta.url)
await mkdir(destination, { recursive: true })
await cp(fileURLToPath(new URL('dist/esm/', source)), fileURLToPath(destination), { recursive: true })
