const {defineConfig} = require('@playwright/test');
const path = require('node:path');
const port = process.env.WEB_TEST_PORT || '8776';
const py = process.env.WEB_TEST_PYTHON || path.resolve(__dirname,'../.agent-loop/web-venv/bin/python');
module.exports = defineConfig({
  testDir:'./tests', workers:1, timeout:45000,
  use:{baseURL:`http://127.0.0.1:${port}`,viewport:{width:1440,height:1000},headless:true,trace:'retain-on-failure'},
  outputDir:'../.agent-loop/web-test-results',
  webServer:{command:`"${py}" "${path.resolve(__dirname,'../tests/web_fixture_server.py')}" --port ${port}`,url:`http://127.0.0.1:${port}`,reuseExistingServer:false,timeout:15000}
});
