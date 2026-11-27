<#
.SYNOPSIS
Step 3 of the M5b deploy: build the app image in Azure Container Registry from the committed
code, tag it with the git commit and print its digest (AC11).

.DESCRIPTION
Refuses to run with uncommitted changes, so the tag names exactly the code in the image. The
build context is `git archive HEAD` unpacked into a scratch folder: only committed files can
reach the image, so the gitignored .env, db/.env and real sample data cannot (D17), whatever
.dockerignore says. `az acr build` builds the Dockerfile's default target (`runtime`) in the
cloud; the in-image OCR tests run locally with `docker build --target test`. Prints the image
reference by digest, which deploy-app.ps1 takes: the app runs exactly this image.

.EXAMPLE
$image = .\infra\build-image.ps1
#>
param(
    [string]$ResourceGroup = 'rg-chatmed-dev',
    [string]$RegistryName = 'crchatmeddev01'
)
. "$PSScriptRoot/common.ps1"

$root = (Resolve-Path "$PSScriptRoot/..").Path
$dirty = Invoke-Native 'git status' { git -C $root status --porcelain }
if ($dirty) {
    throw 'uncommitted changes: commit them first, so the image tag names the code inside it'
}
$commit = Invoke-Native 'git commit' { git -C $root rev-parse HEAD }
$tag = "chatmed:$commit"

$scratch = Join-Path ([System.IO.Path]::GetTempPath()) "chatmed-build-$commit"
$tar = "$scratch.tar"
if (Test-Path $scratch) { Remove-Item -Recurse -Force $scratch }
New-Item -ItemType Directory -Path $scratch | Out-Null
try {
    Invoke-Native 'git archive' { git -C $root archive --format=tar -o $tar HEAD }
    # Windows' own bsdtar: another tar on PATH (Git Bash) reads "C:" as a remote host.
    Invoke-Native 'unpack' { & (Join-Path $env:SystemRoot 'System32\tar.exe') -xf $tar -C $scratch }

    Write-Host "Building $tag in $RegistryName (a few minutes)..."
    Invoke-Native 'az acr build' {
        az acr build -r $RegistryName -g $ResourceGroup -t $tag --platform linux/amd64 $scratch --output none
    }
} finally {
    Remove-Item -Recurse -Force $scratch -ErrorAction SilentlyContinue
    Remove-Item -Force $tar -ErrorAction SilentlyContinue
}

$loginServer = Invoke-Native 'registry' {
    az acr show -n $RegistryName -g $ResourceGroup --query loginServer -o tsv
}
$digest = Invoke-Native 'image digest' {
    az acr repository show -n $RegistryName --image $tag --query digest -o tsv
}
if ($digest -notmatch '^sha256:[0-9a-f]{64}$') {
    throw 'the registry returned no sha256 digest for the new image'
}
$image = "$loginServer/chatmed@$digest"
Write-Host "Built commit $commit"
Write-Host "Image (pass to deploy-app.ps1 -Image): $image"
Write-Output $image
