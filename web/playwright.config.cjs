const {defineConfig} = require('@playwright/test');
const path = require('node:path');
module.exports = defineConfig({
  testDir:'./tests', workers:1, timeout:45000,
  use:{baseURL:'http://127.0.0.1:8776',viewport:{width:1440,height:1000},headless:true,trace:'retain-on-failure'},
  outputDir:'../.agent-loop/web-test-results',
  webServer:{command:`"${path.resolve(__dirname,'../.agent-loop/web-venv/bin/python')}" "${path.resolve(__dirname,'../tests/web_fixture_server.py')}" --port 8776`,url:'http://127.0.0.1:8776',reuseExistingServer:false,timeout:15000}
});
