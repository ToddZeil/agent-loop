import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DualLedger,selectQueries,pageLocale,MARKETS} from '../scripts/seo_lite_collect.mjs';

test('both budgets reserve before the paid operation and receive actual cost',()=>{
  const calls=[];
  const make=name=>({reserve:o=>{calls.push([name,'reserve',o.estimatedCostUsd]);return {id:name};},
    settle:o=>calls.push([name,'settle',o.reservationId,o.actualCostUsd])});
  const ledger=new DualLedger(make('run'),make('daily'));
  const reservation=ledger.reserve({estimatedCostUsd:.004});
  assert.deepEqual(calls,[['run','reserve',.004],['daily','reserve',.004]]);
  ledger.settle({reservationId:reservation.id,actualCostUsd:.002});
  assert.deepEqual(calls.slice(2),[['daily','settle','daily',.002],['run','settle','run',.002]]);
});
test('daily rejection releases run reservation without permitting paid call',()=>{
  const released=[];
  const ledger=new DualLedger({reserve:()=>({id:'a'}),settle:o=>released.push(o)},
    {reserve:()=>{throw Error('daily cap');}});
  assert.throws(()=>ledger.reserve({estimatedCostUsd:.004}),/daily cap/);
  assert.deepEqual(released,[{reservationId:'a',actualCostUsd:0}]);
});
test('run rejection never touches daily ledger',()=>{
  const ledger=new DualLedger({reserve:()=>{throw Error('run cap');}},
    {reserve:()=>assert.fail('daily must not be called')});
  assert.throws(()=>ledger.reserve({estimatedCostUsd:.004}),/run cap/);
});
test('query selection prioritizes evidence and does not repeat paid queries',()=>{
  const row={page:'https://your-pt.com/blog/row/',query:'cable row alternatives',impressions:50,position:12,ctr:.01};
  const rows=[row,{...row,page:'https://your-pt.com/blog/row-two/'},
    {...row,query:'face pull alternatives'}, {...row,query:'your pt'},
    {...row,query:'irrelevant external query',page:'https://external.test/'},
    {...row,query:'unknown demand query',impressions:0}];
  assert.deepEqual(selectQueries(rows,['cable row alternatives']).map(r=>r.query),['face pull alternatives']);
});
test('multilingual queries retain Unicode and cover languages before repeating a market',()=>{
  const row={impressions:20,position:12,ctr:.01};
  const rows=[
    {...row,query:'筋トレ 分割法 週4',page:'https://your-pt.com/ja/blog/split/'},
    {...row,query:'筋肥大 メニュー',page:'https://your-pt.com/ja/blog/plan/'},
    {...row,query:'4분할 루틴',page:'https://your-pt.com/ko/blog/split/'},
    {...row,query:'créer programme',page:'https://your-pt.com/fr/blog/plan/'},
  ];
  const chosen=selectQueries(rows,[],3,['ja','ko','fr']);
  assert.deepEqual(chosen.map(r=>r.locale),['ja','ko','fr']);
  assert.equal(MARKETS[chosen[0].locale].languageCode,'ja');
  assert.equal(MARKETS[chosen[1].locale].locationCode,2410);
});
test('paid query deduplication is market-aware and excludes deceptive hosts',()=>{
  const row={query:'bro split',impressions:20,position:12,ctr:.01};
  const rows=[{...row,page:'https://your-pt.com/fr/blog/split/'},
    {...row,page:'https://your-pt.com/it/blog/split/'},
    {...row,page:'https://your-pt.com.evil.test/fr/blog/split/'}];
  assert.deepEqual(selectQueries(rows,['fr|bro split'],5,['fr','it']).map(r=>r.locale),['it']);
  assert.equal(pageLocale('https://your-pt.com.evil.test/fr/'),null);
});
