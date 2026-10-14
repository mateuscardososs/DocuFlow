import { test, expect, chromium } from '@playwright/test'
import { randomBytes, randomUUID } from 'node:crypto'
import { execFileSync, spawn, spawnSync, type ChildProcess } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

const repoRoot = resolve(import.meta.dirname, '../..')
const python = join(repoRoot, '.venv/bin/python')
const testDbUrl = process.env.TEST_DATABASE_URL
const testRedisUrl = process.env.TEST_REDIS_URL
const enabled = process.env.DOCUFLOW_RUN_BROWSER_E2E === '1'
const serviceConfigMissing = !enabled || !testDbUrl || !testRedisUrl
const skipReason = 'Ignorado: configure TEST_DATABASE_URL, TEST_REDIS_URL e DOCUFLOW_RUN_BROWSER_E2E=1.'

if (serviceConfigMissing) console.info(skipReason)
else if (!existsSync(python)) console.info('Ignorado: instale as dependências Python do projeto em .venv.')
else if (!existsSync(chromium.executablePath())) console.info('Ignorado: instale o Chromium com `npx playwright install chromium`.')

test.skip(serviceConfigMissing, skipReason)
test.skip(!existsSync(python), 'Ignorado: instale as dependências Python do projeto em .venv.')
test.skip(!existsSync(chromium.executablePath()), 'Ignorado: instale o Chromium com `npx playwright install chromium`.')

async function freePort(excluded: number[] = []): Promise<number> {
  while (true) {
    const server = createServer()
    await new Promise<void>((resolveListen, reject) => {
      server.once('error', reject)
      server.listen(0, '127.0.0.1', () => resolveListen())
    })
    const address = server.address()
    if (!address || typeof address === 'string') throw new Error('Não foi possível escolher uma porta local.')
    const port = address.port
    await new Promise<void>((resolveClose, reject) => server.close((error) => error ? reject(error) : resolveClose()))
    if (!excluded.includes(port)) return port
  }
}

async function waitFor(url: string, process: ChildProcess, label: string): Promise<void> {
  const deadline = Date.now() + 30_000
  while (Date.now() < deadline) {
    if (process.exitCode !== null) throw new Error(`${label} encerrou durante a inicialização (código ${process.exitCode}).`)
    try {
      const response = await fetch(url)
      if (response.ok) return
    } catch {
      // O serviço ainda está iniciando.
    }
    await delay(250)
  }
  throw new Error(`${label} não iniciou dentro do limite de 30 segundos.`)
}

function start(command: string, args: string[], env: NodeJS.ProcessEnv, cwd = repoRoot): ChildProcess {
  return spawn(command, args, { cwd, env, stdio: 'ignore' })
}

async function stop(process: ChildProcess | undefined): Promise<void> {
  if (!process || process.exitCode !== null) return
  process.kill('SIGTERM')
  await Promise.race([
    new Promise<void>((resolveStop) => process.once('exit', () => resolveStop())),
    delay(5_000).then(() => { if (process.exitCode === null) process.kill('SIGKILL') }),
  ])
}

test('conclui o ciclo principal no navegador com PostgreSQL, Redis/RQ e ERP demo', async ({ page }) => {
  const check = spawnSync(python, ['-m', 'backend.tests.integration.browser_e2e_support', 'check'], {
    cwd: repoRoot,
    env: process.env,
    encoding: 'utf8',
  })
  if (check.status === 2) test.skip(true, check.stdout.trim() || 'Ignorado: PostgreSQL ou Redis de teste indisponível.')
  if (check.status !== 0) throw new Error(check.stderr || check.stdout || 'A configuração de teste não passou nas verificações de segurança.')

  const runId = randomUUID().replaceAll('-', '').slice(0, 16)
  const email = `browser-${runId}@example.test`
  const password = randomBytes(32).toString('base64url')
  const apiPort = await freePort()
  const frontendPort = await freePort([apiPort])
  const apiUrl = `http://127.0.0.1:${apiPort}`
  const frontendUrl = `http://127.0.0.1:${frontendPort}`
  const runDirectory = mkdtempSync(join(tmpdir(), `docuflow-browser-${runId}-`))
  const documentStorage = join(runDirectory, 'documents')
  const documentQueue = `docuflow_browser_${runId}_documents`
  const erpQueue = `docuflow_browser_${runId}_erp`
  const jwtSecret = randomBytes(48).toString('base64url')
  const testEnv: NodeJS.ProcessEnv = {
    ...process.env,
    DATABASE_URL: testDbUrl,
    DOCUFLOW_TEST_MAIN_DATABASE_URL: process.env.DATABASE_URL ?? '',
    TEST_DATABASE_URL: testDbUrl,
    TEST_REDIS_URL: testRedisUrl,
    DOCUFLOW_TEST_MAIN_REDIS_URL: process.env.REDIS_URL ?? '',
    DOCUFLOW_TEST_MAIN_ERP_REDIS_URL: process.env.ERP_REDIS_URL ?? '',
    REDIS_URL: testRedisUrl,
    ERP_REDIS_URL: testRedisUrl,
    DOCUFLOW_QUEUE_NAME: documentQueue,
    TEST_RQ_QUEUE_NAME: documentQueue,
    DOCUFLOW_ERP_QUEUE_NAME: erpQueue,
    TEST_ERP_RQ_QUEUE_NAME: erpQueue,
    DOCUFLOW_FRONTEND_ORIGIN: frontendUrl,
    DOCUMENT_STORAGE_DIR: documentStorage,
    TEST_DOCUMENT_STORAGE_DIR: documentStorage,
    JWT_SECRET_KEY: jwtSecret,
    DOCUFLOW_TEST_RUN_ID: runId,
    DOCUFLOW_TEST_USER_EMAIL: email,
    DOCUFLOW_TEST_USER_PASSWORD: password,
    DOCUFLOW_TEST_ORDER_REFERENCE: `BROWSER-${runId.toUpperCase()}`,
    DOCUFLOW_TEST_API_URL: apiUrl,
    DOCUFLOW_TEST_PDF_PATH: join(runDirectory, 'invoice.pdf'),
  }

  let apiProcess: ChildProcess | undefined
  let webProcess: ChildProcess | undefined
  let workerProcess: ChildProcess | undefined
  let fixture: { workspace_name: string; order_reference: string; email: string; pdf_base64: string } | undefined
  try {
    const setup = execFileSync(python, ['-m', 'backend.tests.integration.browser_e2e_support', 'setup'], {
      cwd: repoRoot,
      env: testEnv,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    fixture = JSON.parse(setup)

    apiProcess = start(python, ['-m', 'uvicorn', 'backend.tests.integration.browser_api:app', '--host', '127.0.0.1', '--port', String(apiPort)], testEnv)
    workerProcess = start(python, ['-m', 'backend.tests.integration.browser_worker', 'work'], testEnv)
    webProcess = start(join(repoRoot, 'frontend/node_modules/.bin/vite'), ['--host', '127.0.0.1', '--port', String(frontendPort), '--strictPort'], {
      ...testEnv,
      VITE_API_BASE_URL: apiUrl,
    }, join(repoRoot, 'frontend'))

    await waitFor(`${apiUrl}/health`, apiProcess, 'API de teste')
    await waitFor(frontendUrl, webProcess, 'frontend Vite')

    await page.goto(frontendUrl)
    await page.getByLabel('E-mail').fill(fixture.email)
    await page.getByLabel('Senha').fill(password)
    await page.getByRole('button', { name: 'Entrar' }).click()

    await expect(page.getByRole('heading', { name: 'Documentos', exact: true })).toBeVisible()
    await page.getByLabel('Workspace').selectOption({ label: fixture.workspace_name })
    await expect(page.getByText('Aprovador · Operador')).toBeVisible()

    let reviewRequest: { expected_version?: number; justification?: string } | undefined
    page.on('request', (request) => {
      if (request.url().endsWith('/review') && request.method() === 'PATCH') {
        reviewRequest = JSON.parse(request.postData() ?? '{}') as typeof reviewRequest
      }
    })

    await page.getByLabel('Referência do pedido').fill(fixture.order_reference)
    await page.getByLabel('Arquivo PDF, PNG ou JPEG').setInputFiles({
      name: `nota-${runId}.pdf`,
      mimeType: 'application/pdf',
      buffer: Buffer.from(fixture.pdf_base64, 'base64'),
    })
    await page.getByRole('button', { name: 'Enviar para análise' }).click()
    await expect(page.getByRole('status').filter({ hasText: 'recebido e colocado na fila' })).toBeVisible()

    const openDocument = page.getByRole('button', { name: new RegExp(`Abrir nota-${runId}\\.pdf`) })
    await expect(openDocument).toBeVisible({ timeout: 30_000 })
    await expect.poll(async () => {
      await page.getByRole('button', { name: 'Atualizar' }).click()
      return page.getByText('Processado', { exact: true }).count()
    }, { timeout: 60_000 }).toBeGreaterThan(0)
    await openDocument.click()

    await expect(page.getByRole('heading', { name: `nota-${runId}.pdf` })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Achados comerciais' })).toBeVisible()
    await expect(page.getByText('Nenhuma divergência comercial encontrada.')).toBeVisible()
    await expect(page.getByText('Texto nativo', { exact: true })).toBeVisible()
    await expect(page.getByText(fixture.order_reference, { exact: true }).first()).toBeVisible()
    await expect(page.getByText('“Total do documento: R$ 1.250,00”')).toBeVisible()

    await page.getByRole('button', { name: 'Editar dados' }).click()
    await page.getByRole('textbox', { name: 'Item 1 · Descrição valor' }).fill('Arruela zincada M8 conferida')
    await page.getByLabel('Justificativa da correção').fill('Conferência visual pelo operador do teste de navegador.')
    await page.getByRole('button', { name: 'Salvar revisão' }).click()
    await expect(page.getByText('Revisão necessária · v2')).toBeVisible()
    await expect(page.getByText('Conferência visual pelo operador do teste de navegador.').first()).toBeVisible()
    expect(reviewRequest).toMatchObject({
      expected_version: 1,
      justification: 'Conferência visual pelo operador do teste de navegador.',
    })

    await page.getByRole('button', { name: /Aprovar documento/ }).click()
    await expect(page.getByText('Aprovado · v2')).toBeVisible()
    await page.getByRole('button', { name: 'Enviar ao ERP de demonstração' }).click()
    await expect(page.getByText(/ERP-DEMO-/)).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText('Cadastro simulado concluído', { exact: true })).toBeVisible()
    await expect(page.getByText('Versão aprovada: v2')).toBeVisible()
  } finally {
    await stop(webProcess)
    await stop(apiProcess)
    await stop(workerProcess)
    spawnSync(python, ['-m', 'backend.tests.integration.browser_e2e_support', 'cleanup'], {
      cwd: repoRoot,
      env: testEnv,
      stdio: 'ignore',
    })
    rmSync(runDirectory, { recursive: true, force: true })
  }
})
