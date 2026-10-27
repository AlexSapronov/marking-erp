param(
    [Parameter(Mandatory=$true)][string]$BaseUrl,
    [Parameter(Mandatory=$true)][string]$SumatraPath,
    [string]$JournalDirectory = "$env:LOCALAPPDATA\MarkingERP\PrintJournal"
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if (-not $BaseUrl.StartsWith('https://')) { throw 'Trusted HTTPS required.' }
$BaseUrl = $BaseUrl.TrimEnd('/')
$SumatraPath = (Resolve-Path -LiteralPath $SumatraPath).Path
# Use the official signed SumatraPDF installation, never download/execute code from a job.
$signature = Get-AuthenticodeSignature -LiteralPath $SumatraPath
if ($signature.Status -ne 'Valid') { throw 'The PDF renderer must have a valid Authenticode signature.' }
New-Item -ItemType Directory -Force -Path $JournalDirectory | Out-Null
# A single process holds this file for the entire lifetime of this agent.
$lock = [IO.File]::Open((Join-Path $JournalDirectory 'agent.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
$secure = Read-Host 'Printer bearer token (not shown)' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
$headers = @{Authorization="Bearer $token"}
$hasher = [Security.Cryptography.SHA256]::Create()
function Invoke-Erp([string]$Path, $Body) {
    $json = ConvertTo-Json -InputObject $Body -Depth 12 -Compress
    Invoke-RestMethod -Method Post -Uri "$BaseUrl$Path" -Headers $headers -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 45
}
Write-Host 'ERP printer started. Unknown outcomes block further jobs until operator reconciliation.'
try {
    while ($true) {
        try {
            $answer = Invoke-Erp '/api/print-agent/claim' @{}
            $job = $answer.job
            if (-not $job) { Start-Sleep -Seconds 2; continue }
            $id = [Guid]::Parse($job.id).ToString()
            $marker = Join-Path $JournalDirectory ($id + '.claimed')
            if (Test-Path -LiteralPath $marker) { throw 'A local record already exists; do not print again.' }
            # Durable before invoking printer; a crash cannot trigger an automatic second copy.
            $stream = [IO.File]::Open($marker, 'CreateNew', 'Write', 'None')
            $stream.Flush($true); $stream.Dispose()
            $bytes = [Convert]::FromBase64String($job.pdf_base64)
            $sha = ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()
            $outcome = 'unknown'
            $pdf = Join-Path $JournalDirectory ($id + '.pdf')
            try {
                if ($sha -ne $job.pdf_sha256 -or [Text.Encoding]::ASCII.GetString($bytes,0,5) -ne '%PDF-') { throw 'PDF hash/format mismatch.' }
                if ($job.copies -lt 1 -or $job.copies -gt 10 -or $job.printer_name.Contains('"')) { throw 'Invalid print settings.' }
                $printer = Get-Printer -Name $job.printer_name -ErrorAction Stop
                if ($printer.Name -cne $job.printer_name) { throw 'Printer is not an exact match.' }
                [IO.File]::WriteAllBytes($pdf, $bytes)
                $start = New-Object Diagnostics.ProcessStartInfo
                $start.FileName = $SumatraPath
                $start.Arguments = '-silent -print-to "' + $job.printer_name + '" -print-settings "noscale,' + [int]$job.copies + 'x" "' + $pdf + '"'
                $start.UseShellExecute = $false; $start.CreateNoWindow = $true
                $process = [Diagnostics.Process]::Start($start)
                if (-not $process.WaitForExit(120000)) { $process.Kill(); throw 'Print outcome unknown.' }
                if ($process.ExitCode -ne 0) { throw 'Renderer did not confirm submission.' }
                $outcome = 'spooled'
            } finally {
                Remove-Item -LiteralPath $pdf -Force -ErrorAction SilentlyContinue
                # A successful renderer exit confirms submission only, never physical output.
                $receipt = @{job_id=$id;lease=$job.lease;pdf_sha256=$job.pdf_sha256;outcome=$outcome;spool_reference=('sumatra:' + $id)}
                # Retrying this receipt is safe. Do not repeat the print invocation.
                for ($attempt=0; $attempt -lt 3; $attempt++) {
                    try { $null=Invoke-Erp '/api/print-agent/result' $receipt; break }
                    catch { if ($attempt -eq 2) { throw }; Start-Sleep -Seconds 2 }
                }
            }
            Write-Host ('Job ' + $id + ': ' + $outcome + '; physical output requires operator confirmation.')
        } catch {
            Write-Warning 'Printing paused after an error. Check ERP queue and printer. Never automatically repeat an uncertain job.'
            Start-Sleep -Seconds 5
        }
    }
} finally { $lock.Dispose() }
