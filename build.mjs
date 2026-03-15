import { mkdirSync } from 'node:fs';
import esbuild from 'esbuild';

const isWatch = process.argv.includes('--watch');

mkdirSync('agent_log_server/static/dist', { recursive: true });

const common = {
  bundle: true,
  sourcemap: true,
  minify: !isWatch,
  logLevel: 'info',
  external: ['/static/vendor/*', 'https://*'],
};

const codexAgent = {
  ...common,
  entryPoints: ['agent_log_server/static/codex_agent.js'],
  outfile: 'agent_log_server/static/dist/codex_agent.js',
  format: 'esm',
};

if (isWatch) {
  const ctx = await esbuild.context(codexAgent);
  await ctx.watch();
  console.log('[build] watching…');
} else {
  await esbuild.build(codexAgent);
}
