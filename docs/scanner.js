// html5-qrcode を使ったバーコード読み取りモジュール
// (html5-qrcode は <script> CDN で読み込まれて window.Html5Qrcode を提供)

let scanner = null;

const SUPPORTED_FORMATS = () => {
  const F = window.Html5QrcodeSupportedFormats;
  if (!F) return undefined;
  return [F.EAN_13, F.EAN_8, F.UPC_A, F.UPC_E, F.CODE_128];
};

export function isSupported() {
  return typeof window.Html5Qrcode !== 'undefined' && 'mediaDevices' in navigator;
}

export async function startScanner({ elementId, onDetect, onError }) {
  if (!window.Html5Qrcode) {
    onError(new Error('html5-qrcode が読み込まれていません'));
    return;
  }

  scanner = new window.Html5Qrcode(elementId, { verbose: false });
  const config = {
    fps: 10,
    qrbox: { width: 280, height: 140 },
    aspectRatio: 1.333,
    formatsToSupport: SUPPORTED_FORMATS(),
  };

  try {
    await scanner.start(
      { facingMode: 'environment' },
      config,
      (decodedText) => {
        // バーコード検出時 - 数字のみ (JAN-13/8, UPC) を採用
        const code = (decodedText || '').trim();
        if (/^\d{8,14}$/.test(code)) {
          onDetect(code);
        }
      },
      () => { /* per-frame failure: 無視 */ }
    );
  } catch (e) {
    onError(e);
  }
}

export async function stopScanner() {
  if (!scanner) return;
  try {
    if (scanner.isScanning) {
      await scanner.stop();
    }
    await scanner.clear();
  } catch (_) { /* noop */ }
  scanner = null;
}
