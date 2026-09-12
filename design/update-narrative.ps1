$docs = @('architecture-and-workflows.md','graph-generation-and-retrieval.md','code-factory-and-connectors.md','retrieval-strategy.md')
foreach ($name in $docs) {
 $path = Join-Path 'design' $name
 $body = Get-Content -LiteralPath $path -Raw
 $body = $body.Replace('OpenAI Agents SDK coordinator, Claude Agent SDK specialist and Graphify adapter, with application-limited tools','Configurable model profiles, compatible OpenAI/Claude SDK runtimes and Graphify adapter, with application-limited tools')
 $body = $body.Replace('The OpenAI Agents SDK coordinates scoped knowledge analysis and factory work. A Claude Agent SDK specialist performs bounded extraction, code analysis or implementation.','The coordinator and specialists use independently selected compatible model profiles. OpenAI Agents SDK and Claude Agent SDK are supported runtimes for scoped analysis, extraction and implementation; the selected task/model determines the compatible runtime.')
 $body = $body.Replace('dispatches the OpenAI Agents SDK coordinator','resolves the selected generation/coordinator model profile and dispatches a compatible SDK runtime')
 $body = $body.Replace('The coordinator assigns a Claude Agent SDK specialist','The coordinator assigns a specialist with its own selected compatible model/runtime profile')
 $body = $body.Replace('| OpenAI SDK coordinator |','| Coordinator with selected compatible model/runtime |').Replace('| Claude SDK code analyst/implementer |','| Code analyst/implementer with selected compatible model/runtime |')
 $addition = "`n## Model flexibility and developer access`n`n[Model selection policy](model-selection.md) applies to every stage and individual agent: users select compatible profiles within application permissions, with explicit fallbacks and audited execution. Code Factory approval pins the model map; embedding changes rebuild the corresponding index. [Application REST and MCP access](developer-access.md) exposes the same authorized retrieval pipeline to developer code and coding assistants. External calls cannot cross application boundaries or bypass source permissions and Factory approval. The consolidated narrative includes both contracts in full.`n"
 Set-Content -LiteralPath $path -Value ($body.TrimEnd()+"`n"+$addition) -Encoding utf8
}
$masterPath = 'design/Digital-Brain-Design.md'
$old = Get-Content $masterPath -Raw
$prefix = ($old -split '## Part I —',2)[0]
$prefix += "`n**Revision status:** The full narrative now includes configurable models and application REST/MCP endpoints. The six rendered diagrams show the previous validated revision; their updated specifications and the new model-routing specification await rendering/validation. Automatic approval review blocked the validator because of an account usage limit. Previous verification receipts apply only to their recorded artifact hashes.`n`n"
$parts = @(
 @('Part I — SaaS platform, ownership and security','architecture-and-workflows.md'),
 @('Part II — Detailed graph generation and retrieval','graph-generation-and-retrieval.md'),
 @('Part III — Code Factory and application connectors','code-factory-and-connectors.md'),
 @('Part IV — Traditional RAG, NaviRAG and graph RAG','retrieval-strategy.md'),
 @('Part V — Model choice for every application and agent','model-selection.md'),
 @('Part VI — Developer REST APIs and MCP access','developer-access.md')
)
$full = $prefix
foreach ($part in $parts) {
 $section = Get-Content (Join-Path 'design' $part[1]) -Raw
 $section = [regex]::Replace($section,'\A#[^\r\n]*\r?\n','')
 $section = [regex]::Replace($section,'(?m)^(#{2,5}) ', '$1# ')
 $full += "`n## $($part[0])`n`n" + $section.Trim() + "`n"
}
Set-Content $masterPath $full -Encoding utf8
$rendered = (ConvertFrom-Markdown -Path $masterPath).Html
$rendered = [regex]::Replace($rendered,'<pre><code class="language-mermaid">[\s\S]*?</code></pre>','<div class="flow-note">Explore the hierarchy in the <a href="platform-applications.html">platform architecture diagram</a>.</div>')
$rendered = $rendered.Replace('<table>','<div class="table-scroll"><table>').Replace('</table>','</table></div>')
$rendered = [regex]::Replace($rendered,'href="(https://[^"]+)"','href="$1" target="_blank" rel="noopener"')
# ConvertFrom-Markdown repeats IDs for repeated headings; make every anchor unique.
$seen = @{}
$rendered = [regex]::Replace($rendered,'id="([^"]+)"', { param($m) $id=$m.Groups[1].Value; if($seen.ContainsKey($id)){ $seen[$id]++; return 'id="'+$id+'-'+$seen[$id]+'"' }; $seen[$id]=1; return $m.Value })
$toc = '<a class="toc-level-2" href="#diagrams">Architecture diagrams</a>'
foreach ($m in [regex]::Matches($rendered,'<h([23]) id="([^"]+)">([\s\S]*?)</h[23]>')) {
 $toc += '<a class="toc-level-'+$m.Groups[1].Value+'" href="#'+$m.Groups[2].Value+'">'+$m.Groups[3].Value+'</a>'+"`n"
}
$index = Get-Content 'design/full-narrative.html' -Raw
$index = [regex]::Replace($index,'<article class="article" id="narrative">[\s\S]*?</article>',[System.Text.RegularExpressions.MatchEvaluator]{ param($m) '<article class="article" id="narrative">'+$rendered+'</article>' })
$index = [regex]::Replace($index,'<nav class="toc" aria-label="Narrative contents">[\s\S]*?</nav>',[System.Text.RegularExpressions.MatchEvaluator]{ param($m) '<nav class="toc" aria-label="Narrative contents">'+$toc+'</nav>' })
$index = $index.Replace('Six checked diagrams','Six prior-revision diagrams<br>Updated diagram validation pending')
$index = $index.Replace('Each diagram opens in its own interactive viewer with zoom, themes and export.','These viewers show the previous validated revision. Model-selection and REST/MCP changes are complete in the narrative below; updated diagram rendering is pending because automatic approval review hit an account usage limit.')
$index = $index.Replace('Chat, Code Factory and application-specific integrations.','Chat, Code Factory, configurable models and application-specific REST/MCP integrations.')
Set-Content 'design/full-narrative.html' $index -Encoding utf8
$ids = @([regex]::Matches($index,'id="([^"]+)"') | ForEach-Object {$_.Groups[1].Value})
$missing = @()
foreach($m in [regex]::Matches($index,'href="([^"]+)"')) { $href=$m.Groups[1].Value; if($href.StartsWith('#')) {if($ids -notcontains $href.Substring(1)){$missing += $href}} elseif($href -notmatch '^[a-z]+:'){if(-not(Test-Path (Join-Path 'design' ($href -split '#')[0]))){$missing += $href}} }
$result = [ordered]@{revision='model-selection-and-developer-access';indexSha256=(Get-FileHash 'design/full-narrative.html').Hash;staticLinksPassed=($missing.Count -eq 0);missing=$missing;duplicateIds=@($ids | Group-Object | Where-Object Count -gt 1 | Select-Object -ExpandProperty Name);diagramValidation='pending: automatic approval review usage limit';visualReview='not performed for this revision; previous screenshots are historical'}
$result | ConvertTo-Json -Depth 5 | Set-Content 'design/revision-verification.json' -Encoding utf8
$result | ConvertTo-Json -Depth 5

