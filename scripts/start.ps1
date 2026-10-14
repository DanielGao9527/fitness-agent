param([int]$Port = 8765, [switch]$EnableQwen, [switch]$EnableSpeech, [switch]$EnableNutrition, [switch]$EnableWorkouts, [switch]$EnableKnowledge, [switch]$EnablePlans, [switch]$EnableTrainingPlans, [switch]$EnableCoach, [switch]$EnablePhoto, [switch]$EnableHybridRetrieval, [switch]$UseLexicalBaseline, [string]$PythonPath, [string]$DatabasePath, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentPython = if ($PythonPath) { $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($PythonPath) } else { Join-Path $projectRoot '.venv\Scripts\python.exe' }
if ($DatabasePath) { $DatabasePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DatabasePath) }
if (-not (Test-Path -LiteralPath $environmentPython)) { throw 'Run .\scripts\setup.ps1 first.' }
if ($Port -lt 1024 -or $Port -gt 65515) { throw 'Choose a port between 1024 and 65515.' }
if ($EnableHybridRetrieval -and $UseLexicalBaseline) { throw 'Choose one retrieval profile.' }
if (($EnableQwen -or $EnableSpeech -or $EnableNutrition -or $EnableWorkouts -or $EnableKnowledge -or $EnablePlans -or $EnableTrainingPlans -or $EnableCoach -or $EnablePhoto -or $EnableHybridRetrieval) -and [string]::IsNullOrWhiteSpace($env:DASHSCOPE_API_KEY)) {
    throw 'DASHSCOPE_API_KEY is missing. Set it privately in your local environment first.'
}
if ($CheckOnly) {
    [pscustomobject]@{ ApplicationDirectory=$projectRoot; Python=$environmentPython; Database=$DatabasePath; Port=$Port; RetrievalMode=$(if ($UseLexicalBaseline) { 'lexical' } else { 'hybrid' }); Tokenizer=$(if ($UseLexicalBaseline) { 'bigram' } else { 'jieba' }); ModelKeyConfigured=(-not [string]::IsNullOrWhiteSpace($env:DASHSCOPE_API_KEY)); ModelProvider=$env:FITNESS_MODEL_PROVIDER; CoachRequested=[bool]$EnableCoach; KnowledgeRequested=[bool]$EnableKnowledge } | ConvertTo-Json -Compress
    return
}
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
try { $listener.Start() } catch [System.Net.Sockets.SocketException] { throw "Port $Port is already in use. Stop the known Fitness service or explicitly choose another port; no second service was started." } finally { $listener.Stop() }
$availablePort = $Port
Push-Location -LiteralPath $projectRoot
$previousDatabase = $env:FITNESS_DB_PATH
$previousProvider = $env:FITNESS_MODEL_PROVIDER
$previousSpeech = $env:FITNESS_SPEECH_ENABLED
$previousNutrition = $env:FITNESS_NUTRITION_ENABLED
$previousWorkout = $env:FITNESS_WORKOUT_ENABLED
$previousKnowledge = $env:FITNESS_KNOWLEDGE_QA_ENABLED
$previousPlans = $env:FITNESS_MEAL_PLAN_ENABLED
$previousTrainingPlans = $env:FITNESS_TRAINING_PLAN_ENABLED
$previousCoach = $env:FITNESS_COACH_ENABLED
$previousPhoto = $env:FITNESS_PHOTO_ENABLED
$previousRetrievalMode = $env:FITNESS_RETRIEVAL_MODE
$previousRetrievalTokenizer = $env:FITNESS_RETRIEVAL_TOKENIZER
try {
    if ($DatabasePath) { $env:FITNESS_DB_PATH = $DatabasePath }
    $env:FITNESS_RETRIEVAL_MODE = if ($UseLexicalBaseline) { 'lexical' } else { 'hybrid' }
    $env:FITNESS_RETRIEVAL_TOKENIZER = if ($UseLexicalBaseline) { 'bigram' } else { 'jieba' }
    if ($EnableQwen -or $EnableNutrition -or $EnableWorkouts -or $EnableKnowledge -or $EnablePlans -or $EnableTrainingPlans -or $EnableCoach -or $EnablePhoto) { $env:FITNESS_MODEL_PROVIDER = 'qwen' }
    if ($EnablePhoto) { $env:FITNESS_PHOTO_ENABLED = 'true' }
    if ($EnableSpeech) { $env:FITNESS_SPEECH_ENABLED = 'true' }
    if ($EnableNutrition) { $env:FITNESS_NUTRITION_ENABLED = 'true' }
    if ($EnableWorkouts) { $env:FITNESS_WORKOUT_ENABLED = 'true' }
    if ($EnableKnowledge) { $env:FITNESS_KNOWLEDGE_QA_ENABLED = 'true' }
    if ($EnablePlans) { $env:FITNESS_MEAL_PLAN_ENABLED = 'true' }
    if ($EnableTrainingPlans) { $env:FITNESS_TRAINING_PLAN_ENABLED = 'true' }
    if ($EnableCoach) { $env:FITNESS_COACH_ENABLED = 'true' }
    Write-Output "Fitness Agent: http://127.0.0.1:$availablePort"
    & $environmentPython -m uvicorn app:app --host 127.0.0.1 --port $availablePort --no-access-log --no-proxy-headers
    if ($LASTEXITCODE -ne 0) { throw 'Fitness did not start successfully; inspect the local error output.' }
} finally {
    if ($null -eq $previousDatabase) { Remove-Item Env:FITNESS_DB_PATH -ErrorAction SilentlyContinue }
    else { $env:FITNESS_DB_PATH = $previousDatabase }
    if ($null -eq $previousRetrievalMode) { Remove-Item Env:FITNESS_RETRIEVAL_MODE -ErrorAction SilentlyContinue }
    else { $env:FITNESS_RETRIEVAL_MODE = $previousRetrievalMode }
    if ($null -eq $previousRetrievalTokenizer) { Remove-Item Env:FITNESS_RETRIEVAL_TOKENIZER -ErrorAction SilentlyContinue }
    else { $env:FITNESS_RETRIEVAL_TOKENIZER = $previousRetrievalTokenizer }
    if ($EnablePhoto) {
        if ($null -eq $previousPhoto) { Remove-Item Env:FITNESS_PHOTO_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_PHOTO_ENABLED = $previousPhoto }
    }
    if ($EnableSpeech) {
        if ($null -eq $previousSpeech) { Remove-Item Env:FITNESS_SPEECH_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_SPEECH_ENABLED = $previousSpeech }
    }
    if ($EnableNutrition) {
        if ($null -eq $previousNutrition) { Remove-Item Env:FITNESS_NUTRITION_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_NUTRITION_ENABLED = $previousNutrition }
    }
    if ($EnableWorkouts) {
        if ($null -eq $previousWorkout) { Remove-Item Env:FITNESS_WORKOUT_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_WORKOUT_ENABLED = $previousWorkout }
    }
    if ($EnableKnowledge) {
        if ($null -eq $previousKnowledge) { Remove-Item Env:FITNESS_KNOWLEDGE_QA_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_KNOWLEDGE_QA_ENABLED = $previousKnowledge }
    }
    if ($EnablePlans) {
        if ($null -eq $previousPlans) { Remove-Item Env:FITNESS_MEAL_PLAN_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_MEAL_PLAN_ENABLED = $previousPlans }
    }
    if ($EnableTrainingPlans) {
        if ($null -eq $previousTrainingPlans) { Remove-Item Env:FITNESS_TRAINING_PLAN_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_TRAINING_PLAN_ENABLED = $previousTrainingPlans }
    }
    if ($EnableCoach) {
        if ($null -eq $previousCoach) { Remove-Item Env:FITNESS_COACH_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_COACH_ENABLED = $previousCoach }
    }
    if ($EnableQwen -or $EnableNutrition -or $EnableWorkouts -or $EnableKnowledge -or $EnablePlans -or $EnableTrainingPlans -or $EnableCoach -or $EnablePhoto) {
        if ($null -eq $previousProvider) { Remove-Item Env:FITNESS_MODEL_PROVIDER -ErrorAction SilentlyContinue }
        else { $env:FITNESS_MODEL_PROVIDER = $previousProvider }
    }
    Pop-Location
}
