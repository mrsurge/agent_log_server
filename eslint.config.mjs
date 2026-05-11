import tsParser from '@typescript-eslint/parser';
import tsPlugin from '@typescript-eslint/eslint-plugin';

export default [
  {
    ignores: [
      'rust/crates/als-server/src/static/dist/**',
      'rust/crates/als-server/src/static/vendor/**',
    ],
  },
  {
    files: ['rust/crates/als-server/src/static/**/*.ts'],
    languageOptions: {
      parser: tsParser,
      ecmaVersion: 'latest',
      sourceType: 'module',
    },
    plugins: {
      '@typescript-eslint': tsPlugin,
    },
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
    },
  },
];
