// Trusted collector for seo_lite.py. Paid calls are serialized and double-ledgered.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

export class DualLedger {
  constructor(run, shared) { this.run = run; this.shared = shared; }
  reserve(options) {
    const local = this.run.reserve(options);
    try { return { id: { local: local.id, shared: this.shared.reserve(options).id } }; }
    catch (error) {
      this.run.settle({ reservationId: local.id, actualCostUsd: 0 });
      throw error;
    }
  }
  settle({ reservationId, ...options }) {
    // If settlement is interrupted, the unsettled upper bound remains counted.
    this.shared.settle({ reservationId: reservationId.shared, ...options });
    this.run.settle({ reservationId: reservationId.local, ...options });
  }
}

export const MARKETS = {
  en:{languageCode:'en',locationCode:2840,countryCode:'US'},
  es:{languageCode:'es',locationCode:2724,countryCode:'ES'},
  de:{languageCode:'de',locationCode:2276,countryCode:'DE'},
  fr:{languageCode:'fr',locationCode:2250,countryCode:'FR'},
  pt:{languageCode:'pt',locationCode:2620,countryCode:'PT'},
  ja:{languageCode:'ja',locationCode:2392,countryCode:'JP'},
  ko:{languageCode:'ko',locationCode:2410,countryCode:'KR'},
  it:{languageCode:'it',locationCode:2380,countryCode:'IT'},
  zh:{languageCode:'zh',locationCode:2156,countryCode:'CN'},
  ar:{languageCode:'ar',locationCode:2682,countryCode:'SA'},
  hi:{languageCode:'hi',locationCode:2356,countryCode:'IN'},
  id:{languageCode:'id',locationCode:2360,countryCode:'ID'},
};

export function pageLocale(page) {
  try {
    const url=new URL(page);
    if (url.origin!=='https://your-pt.com') return null;
    const prefix=url.pathname.split('/')[1];
    return MARKETS[prefix] ? prefix : 'en';
  } catch { return null; }
}

export function selectQueries(rows, seen, count = 12, locales = ['en']) {
  const ranked = rows.map(r=>({...r,locale:pageLocale(r.page)})).filter(r =>
    locales.includes(r.locale) && r.impressions >= 5 && r.position >= 4 &&
    /^[\p{L}\p{N}\s ,?？'’()\-]+$/u.test(r.query ?? '') &&
    (r.query ?? '').length >= 3 && (r.query ?? '').length <= 160 && !/your[ -]?pt/i.test(r.query))
    .sort((a,b) => b.impressions * Math.min(b.position, 30) * (1-b.ctr) -
      a.impressions * Math.min(a.position, 30) * (1-a.ctr));
  const unique = new Set(seen);
  const queues = new Map(locales.map(locale=>[locale,[]]));
  for (const row of ranked) {
    const query=row.query.normalize('NFKC').trim().toLowerCase();
    const key=`${row.locale}|${query}`;
    if (unique.has(key) || (row.locale==='en' && unique.has(query))) continue;
    unique.add(key); queues.get(row.locale).push(row);
  }
  // Cover different languages before buying a second SERP in the same market.
  const selected=[];
  while (selected.length<count) {
    let added=false;
    for (const locale of locales) {
      const row=queues.get(locale).shift();
      if (row) {selected.push(row);added=true;}
      if (selected.length>=count) break;
    }
    if (!added) break;
  }
  return selected;
}

const write = (file, data) => {
  fs.mkdirSync(path.dirname(file), {recursive:true});
  fs.writeFileSync(file, JSON.stringify(data,null,2)+'\n');
};

async function main() {
  const control = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const {repo, worktree, run_dir: runDir, deadline, budget_usd: cap} = control;
  const load = name => import(pathToFileURL(path.join(repo,'scripts/lib',name)).href);
  const [{BudgetLedger}, {DataForSeoClient}, {loadDotEnv}, gsc] = await Promise.all([
    load('provider-budget.mjs'), load('dataforseo-client.mjs'), load('env.mjs'), load('gsc.mjs')]);
  loadDotEnv(repo);
  const run = new BudgetLedger({file:path.join(runDir,'dataforseo-spend.json'),
    rollingCapUsd:cap, windowMs:100*365*86400000});
  const shared = new BudgetLedger({file:path.join(repo,'docs/seo/.cache/dataforseo-spend.json')});
  for (const budget of [run,shared]) {
    const originalLoad = budget.load.bind(budget);
    budget.load = () => {
      if (fs.existsSync(budget.file)) {
        const data=JSON.parse(fs.readFileSync(budget.file,'utf8'));
        if (!Array.isArray(data.entries) || !Array.isArray(data.reservations) ||
            [...data.entries,...data.reservations].some(e=>!Number.isFinite(Number(e.costUsd ?? e.estimatedCostUsd)) || Number(e.costUsd ?? e.estimatedCostUsd)<0))
          throw Error('Invalid spend ledger; paid requests blocked');
      }
      return originalLoad();
    };
  }
  const ledger = new DualLedger(run, shared);
  const stateFile = path.join(runDir,'research-state.json');
  const state = fs.existsSync(stateFile) ? JSON.parse(fs.readFileSync(stateFile,'utf8')) : {seen:[],cycle:0};
  const locales=(control.locales ?? ['en']).filter(locale=>MARKETS[locale]);
  const report = {fetchedAt:new Date().toISOString(),cycle:++state.cycle,
    markets:locales.map(locale=>({locale,...MARKETS[locale]})),serps:[],errors:[]};
  const gscFile = path.join(runDir,'gsc-query-page.json');
  if (!fs.existsSync(gscFile) || Date.now()-fs.statSync(gscFile).mtimeMs > 4*3600000) {
    try {
      const result = await gsc.queryAnalyticsWithMetadata(repo,{dimensions:['query','page'],days:28,rowLimit:25000});
      write(gscFile,{fetchedAt:new Date().toISOString(),...result});
      report.gsc = {status:'fresh',rows:result.rows.length,window:result.window,metadata:result.metadata};
    } catch(error) { report.errors.push('GSC: '+error.message.slice(0,400)); }
  }
  const firstParty = fs.existsSync(gscFile) ? JSON.parse(fs.readFileSync(gscFile,'utf8')) : null;
  if (firstParty) {
    report.gsc ??= {status:'cached',fetchedAt:firstParty.fetchedAt,window:firstParty.window,rows:firstParty.rows.length};
    write(path.join(worktree,'docs/seo/gsc',`${control.id}-query-page.json`),firstParty);
    report.gsc.scope='Property-wide query/page metrics, without country or device filtering. SERP markets are separate.';
    report.localeEvidence=Object.fromEntries(locales.map(locale=>{
      const rows=firstParty.rows.filter(row=>pageLocale(row.page)===locale);
      return [locale,{queryPageRows:rows.length,impressions:rows.reduce((sum,row)=>sum+row.impressions,0)}];
    }));
  }
  const fallback = [];
  if (!firstParty?.rows?.length) {
    // Only incumbent slugs; fallback names are hypotheses, never demand evidence.
    const dir = path.join(worktree,'src/content/blog');
    if (fs.existsSync(dir)) for (const f of fs.readdirSync(dir).sort()) {
      if (!f.endsWith('.md') || /[^a-z0-9.-]/.test(f)) continue;
      fallback.push({query:f.replace(/\.md$/,'').replaceAll('-',' '),
        page:'https://your-pt.com/blog/'+f.replace(/\.md$/,'')+'/',impressions:5,position:10,ctr:0});
    }
    report.fallback = 'Incumbent slug hypotheses; GSC evidence unavailable.';
  }
  const offset=(state.cycle-1)%locales.length;
  const rotated=[...locales.slice(offset),...locales.slice(0,offset)];
  const queries = selectQueries(firstParty?.rows?.length ? firstParty.rows : fallback,
    state.seen,control.queries_per_cycle ?? 12,rotated);
  const client = new DataForSeoClient({rootDir:worktree,ledger,retries:0,timeoutMs:25000});
  for (const row of queries) {
    if (Date.now()/1000 >= deadline) { report.stop='deadline'; break; }
    // Published depth-10 live price is $0.002. Reserve twice that and no extras.
    if (!run.status({requestedUsd:0.004}).allowed) { report.stop='run_budget'; break; }
    if (!shared.status({requestedUsd:0.004}).allowed) { report.stop='daily_budget'; break; }
    const query = row.query.normalize('NFKC').trim().toLowerCase();
    const market={locale:row.locale,...MARKETS[row.locale]};
    const artifact = `docs/seo/raw/dataforseo/${control.id}-${state.cycle}-${report.serps.length+1}.json`;
    state.seen.push(`${row.locale}|${query}`); write(stateFile,state); // ambiguous failures must not auto-repeat
    try {
      const result = await client.request({operation:`${control.id}_cycle_${state.cycle}`,
        endpoint:'/serp/google/organic/live/advanced',
        payload:[{keyword:query,location_code:market.locationCode,language_code:market.languageCode,depth:10,device:'mobile',os:'android'}],
        market,estimatedCostUsd:0.004,outputArtifact:artifact});
      const items = result.data.tasks?.[0]?.result?.[0]?.items ?? [];
      report.serps.push({query,market,incumbent:row.page,gsc:{impressions:row.impressions,position:row.position,ctr:row.ctr},
        taskId:result.task.id,artifact,costUsd:result.task.actualCostUsd,
        organic:items.filter(i=>i.type==='organic').map(i=>({position:i.rank_group,url:i.url,title:i.title,description:i.description})),
        related:items.filter(i=>['people_also_ask','related_searches'].includes(i.type))});
    } catch(error) {
      report.errors.push(query+': '+error.message.slice(0,400));
      // Authentication, network or estimator failures should never burn a batch.
      break;
    }
  }
  report.spend=run.status();
  write(stateFile,state);
  const output = path.join(worktree,`docs/seo/reports/${control.id}-cycle-${state.cycle}.json`);
  write(output,report);
  write(path.join(runDir,'latest-research.json'),report);
  console.log(JSON.stringify({report:output,cycle:state.cycle,serps:report.serps.length,
    gsc:report.gsc,spend:report.spend,stop:report.stop,errors:report.errors}));
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url)
  main().catch(error=>{console.error(error.message);process.exitCode=1;});
