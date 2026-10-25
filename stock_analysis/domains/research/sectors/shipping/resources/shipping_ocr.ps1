param([Parameter(Mandatory=$true)][string]$ImagePath, [string]$Language = 'zh-Hans-CN')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
$shippingAsTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Wait-ShippingOperation($Operation, $ResultType) {
    $shippingTask = $shippingAsTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $shippingTask.GetAwaiter().GetResult()
}
$shippingFile = Wait-ShippingOperation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
$shippingStream = Wait-ShippingOperation ($shippingFile.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
try {
    $shippingDecoder = Wait-ShippingOperation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($shippingStream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $shippingBitmap = Wait-ShippingOperation ($shippingDecoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    try {
        $shippingEngine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Language))
        if ($null -eq $shippingEngine) { throw "Installed OCR language unavailable: $Language" }
        $shippingResult = Wait-ShippingOperation ($shippingEngine.RecognizeAsync($shippingBitmap)) ([Windows.Media.Ocr.OcrResult])
        $shippingWords = @(foreach ($shippingLine in $shippingResult.Lines) {
            foreach ($shippingWord in $shippingLine.Words) {
                @{ text=$shippingWord.Text; x=$shippingWord.BoundingRect.X; y=$shippingWord.BoundingRect.Y;
                   width=$shippingWord.BoundingRect.Width; height=$shippingWord.BoundingRect.Height }
            }
        })
        @{ language=$Language; text=$shippingResult.Text; words=$shippingWords } | ConvertTo-Json -Depth 5 -Compress
    } finally { $shippingBitmap.Dispose() }
} finally { $shippingStream.Dispose() }
