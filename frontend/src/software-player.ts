import type AVPlayer from '@libmedia/avplayer/AVPlayer'

export type SoftwarePlayer = AVPlayer

// libmedia keeps its worker/chunk URLs relative to this module. The build
// copies its unmodified distribution beside the locally bundled Wasm files.
export async function createSoftwarePlayer(container: HTMLDivElement): Promise<SoftwarePlayer> {
  const moduleUrl = '/libmedia/1.3.1/js/avplayer.js'
  const { default: Player } = await import(/* @vite-ignore */ moduleUrl) as { default: typeof AVPlayer }
  return new Player({
    container,
    wasmBaseUrl: new URL('/libmedia/1.3.1/wasm', location.href).href,
    enableHardware: false,
    enableWebCodecs: false,
    checkUseMSE: () => false,
    enableWorker: true,
    enableWebGPU: false,
    preLoadTime: 2,
  })
}
