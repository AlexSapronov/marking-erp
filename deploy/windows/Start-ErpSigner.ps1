param(
    [Parameter(Mandatory=$true)][string]$BaseUrl,
    [Parameter(Mandatory=$true)][string]$Thumbprint,
    [switch]$Use32Bit
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if (-not $BaseUrl.StartsWith('https://')) { throw 'ERP requires trusted HTTPS.' }
$BaseUrl = $BaseUrl.TrimEnd('/')
$Thumbprint = ($Thumbprint -replace '\s','').ToUpperInvariant()
$certificate = Get-Item "Cert:\CurrentUser\My\$Thumbprint"
$hasher = [Security.Cryptography.SHA256]::Create()
$certSha = ([BitConverter]::ToString($hasher.ComputeHash($certificate.RawData))).Replace('-','').ToLowerInvariant()
$secure = Read-Host 'Signer bearer token (not shown)' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
$headers = @{Authorization="Bearer $token"}
$cscript = if ($Use32Bit) { "$env:WINDIR\SysWOW64\cscript.exe" } else { "$env:WINDIR\System32\cscript.exe" }
$helper = Join-Path $PSScriptRoot 'Sign-Payload.vbs'
function Invoke-Erp([string]$Path, $Body) {
    $json = ConvertTo-Json -InputObject $Body -Depth 30 -Compress
    Invoke-RestMethod -Method Post -Uri "$BaseUrl$Path" -Headers $headers -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 45
}
Write-Host 'ERP signer started. Stop with Ctrl+C. Private key stays on the token.'
while ($true) {
    try {
        $answer = Invoke-Erp '/api/signer/claim' @{}
        $job = $answer.job
        if (-not $job) { Start-Sleep -Seconds 2; continue }
        if ($job.certificate_sha256 -ne $certSha) { throw 'Pinned certificate differs from the selected certificate.' }
        $payload = [Convert]::FromBase64String($job.payload_base64)
        $payloadSha = ([BitConverter]::ToString($hasher.ComputeHash($payload))).Replace('-','').ToLowerInvariant()
        if ($payloadSha -ne $job.payload_sha256) { throw 'Payload hash mismatch.' }
        $start = New-Object Diagnostics.ProcessStartInfo
        $start.FileName = $cscript
        $start.Arguments = '//nologo "' + $helper + '" ' + $Thumbprint + ' detached'
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardInput = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $start
        $null = $process.Start()
        $process.StandardInput.WriteLine($job.payload_base64)
        $process.StandardInput.Close()
        $signature = $process.StandardOutput.ReadToEnd().Trim()
        $diagnostic = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw 'CryptoPro signing/chain/VerifyCades failed. Check local CryptoPro and token.' }
        $signatureBytes = [Convert]::FromBase64String($signature)
        $sigSha = ([BitConverter]::ToString($hasher.ComputeHash($signatureBytes))).Replace('-','').ToLowerInvariant()
        $receipt = Invoke-Erp '/api/signer/result' @{
            job_id=$job.job_id;fence=$job.fence;lease=$job.lease;
            payload_sha256=$payloadSha;certificate_sha256=$certSha;signature_base64=$signature
        }
        if ($receipt.accepted) {
            $proof = Invoke-Erp '/api/signer/verification' @{
                job_id=$job.job_id;fence=$job.fence;payload_sha256=$payloadSha;
                signature_sha256=$sigSha;certificate_sha256=$certSha;cades_valid=$true;chain_valid=$true
            }
            Write-Host ('Job ' + $job.job_id + ' verified: ' + $proof.verified)
        }
    } catch {
        # Never log request URLs/bodies, tokens, complete codes or signatures.
        Write-Warning 'Signer paused after an error. Check ERP diagnostics and local CryptoPro. No external request is repeated here.'
        Start-Sleep -Seconds 5
    }
}
