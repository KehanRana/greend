import { act,cleanup,fireEvent,render,screen,waitFor,within } from '@testing-library/react';
import { afterEach,beforeEach,describe,expect,it,vi } from 'vitest';
import App from './App';

const key='test-client-key';
let failedMetrics=false,cancelled=false;
let posts:{path:string;key:string|null;body:unknown}[]=[];
let batchFailures=0;
const regions={mode:'demo',evaluated_at:new Date().toISOString(),poller:{status:'ok',last_error:null},items:[
  {endpoint_id:'us-east-1',region:'us-east-1',grid_zone:'demo:virginia',healthy:true,locality:'simulated',signal_status:'fresh',intensity_gco2e_kwh:400,source:'demo-fixture-v1',observed_at:new Date().toISOString(),retrieved_at:new Date().toISOString()},
  {endpoint_id:'eu-central-1',region:'eu-central-1',grid_zone:'demo:frankfurt',healthy:true,locality:'simulated',signal_status:'fresh',intensity_gco2e_kwh:80,source:'demo-fixture-v1',observed_at:new Date().toISOString(),retrieved_at:new Date().toISOString()},
  {endpoint_id:'ap-southeast-2',region:'ap-southeast-2',grid_zone:'demo:sydney',healthy:true,locality:'simulated',signal_status:'fresh',intensity_gco2e_kwh:500,source:'demo-fixture-v1',observed_at:new Date().toISOString(),retrieved_at:new Date().toISOString()}
]};
function metric(){return {mode:'demo',succeeded_executions:2,compared_executions:2,excluded_executions:0,excluded_reasons:{},estimated_actual_g:.4,estimated_baseline_g:1.6,estimated_difference_g:1.2,compared_tokens:4000,estimated_difference_g_per_1000_tokens:.3,estimation_groups:[{baseline_endpoint:'us-east-1',energy_model_version:'illustrative-v1',kwh_per_1000_tokens:.001,model_version:'fixture-v1',executions:2}],owner_job_states:cancelled?{cancelled:1}:{waiting_for_carbon:1},outcomes:{succeeded:2},note:'Simulation',configuration:{baseline_endpoint:'us-east-1',energy_model_version:'illustrative-v1',kwh_per_1000_tokens:.001}};}
function response(body:unknown,status=200){return Promise.resolve(new Response(JSON.stringify(body),{status,headers:{'content-type':'application/json'}}));}
async function connect(){fireEvent.change(screen.getByLabelText('Gateway key'),{target:{value:key}});fireEvent.click(screen.getByRole('button',{name:'Connect'}));await screen.findByRole('button',{name:'Disconnect'});await screen.findByText('+1.200000');}

beforeEach(()=>{
  failedMetrics=false;cancelled=false;posts=[];batchFailures=0;
  Object.defineProperty(HTMLDialogElement.prototype,'showModal',{configurable:true,value:function(){this.setAttribute('open','');}});
  Object.defineProperty(HTMLDialogElement.prototype,'close',{configurable:true,value:function(){if(this.hasAttribute('open')){this.removeAttribute('open');this.dispatchEvent(new Event('close'));}}});
  vi.stubGlobal('fetch',vi.fn((path:string,init:RequestInit={})=>{
    if(init.method==='POST'){
      posts.push({path,key:new Headers(init.headers).get('authorization'),body:JSON.parse(String(init.body||'{}'))});
      if(path.endsWith('/cancel')){cancelled=true;return response({status:'cancelled'});}
      if(path==='/api/jobs'){if(batchFailures-->0)return response({error:{message:'Storage temporarily unavailable'}},503);return response({id:'job-demo',status:'queued',status_url:'/api/jobs/job-demo'},202);}
      return response({scenario:'clean',mode:'demo',revision:'new',refreshed:true});
    }
    if(path==='/api/metrics')return failedMetrics?response({error:{message:'Storage unavailable'}},503):response(metric());
    if(path==='/api/regions')return response(regions);
    if(path==='/api/demo/scenario')return response({scenario:'high',mode:'demo',revision:'first'});
    if(path.startsWith('/api/jobs'))return response({items:cancelled?[]:[{id:'job_abcdefghi',state:'waiting_for_carbon',created_at:new Date().toISOString(),deadline_at:new Date(Date.now()+300000).toISOString(),attempt_count:0,carbon_threshold:150}]});
    return response({items:[]});
  }));
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.restoreAllMocks();});

describe('dashboard',()=>{
  it('connects without storing credentials and renders API-derived estimates',async()=>{
    const storage=vi.spyOn(Storage.prototype,'setItem');
    render(<App/>);
    expect(fetch).not.toHaveBeenCalled();
    await connect();
    expect(screen.getByText('1.600000')).toBeInTheDocument();
    expect(screen.getByText('0.400000')).toBeInTheDocument();
    expect(screen.getByRole('button',{name:/Frankfurt eu-central-1 80/})).toBeInTheDocument();
    expect(storage).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain(key);
    fireEvent.click(screen.getByRole('button',{name:'Disconnect'}));
    expect(screen.getByLabelText('Gateway key')).toHaveValue('');
  });
  it('retains last-good values when refresh fails',async()=>{
    render(<App/>);await connect();failedMetrics=true;
    fireEvent.click(screen.getByRole('button',{name:'Refresh'}));
    await screen.findByText(/Refresh failed. Showing data from/);
    expect(screen.getByText('+1.200000')).toBeInTheDocument();
  });
  it('cancels only through the owner-authorized API and updates the queue',async()=>{
    render(<App/>);await connect();
    fireEvent.click(await screen.findByRole('button',{name:/Cancel job/}));
    await screen.findByText('Job cancelled before dispatch.');
    await waitFor(()=>expect(screen.queryByRole('button',{name:/Cancel job/})).not.toBeInTheDocument());
    expect(posts[0].path).toBe('/api/jobs/job_abcdefghi/cancel');expect(posts[0].key).toBe(`Bearer ${key}`);
  });
  it('reuses keys and bodies when a batch submission is unconfirmed',async()=>{
    render(<App/>);await connect();
    fireEvent.click(screen.getByRole('button',{name:/Submit demo batch/}));
    const dialog=screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Number of jobs'),{target:{value:'2'}});batchFailures=1;
    fireEvent.click(within(dialog).getByRole('button',{name:'Submit batch'}));
    await within(dialog).findByRole('button',{name:'Retry unconfirmed'});
    const firstBody=posts[0].body;
    const firstKey=new Headers((vi.mocked(fetch).mock.calls.find(([p,init])=>p==='/api/jobs'&&init?.method==='POST')![1]!).headers).get('idempotency-key');
    fireEvent.click(within(dialog).getByRole('button',{name:'Retry unconfirmed'}));
    await within(dialog).findByText('2 submissions confirmed. Follow their progress in your queue.');
    expect(posts[2].body).toEqual(firstBody);
    const submissions=vi.mocked(fetch).mock.calls.filter(([p,init])=>p==='/api/jobs'&&init?.method==='POST');
    expect(new Headers(submissions[2][1]!.headers).get('idempotency-key')).toBe(firstKey);
  });
  it('opens navigation without leaving obscured content focusable',()=>{
    render(<App/>);const menu=screen.getByRole('button',{name:'Menu'});
    fireEvent.click(menu);expect(menu).toHaveAttribute('aria-expanded','true');
    expect(document.querySelector('main')).toHaveAttribute('inert');
    fireEvent.keyDown(window,{key:'Escape'});expect(menu).toHaveAttribute('aria-expanded','false');
    expect(document.querySelector('main')).not.toHaveAttribute('inert');expect(menu).toHaveFocus();
  });
  it('returns keyboard focus to the trigger after dismissing a dialog',async()=>{
    render(<App/>);await connect();
    const trigger=screen.getByRole('button',{name:'Method'});trigger.focus();fireEvent.click(trigger);
    fireEvent(screen.getByRole('dialog'),new Event('cancel',{bubbles:true,cancelable:true}));
    await waitFor(()=>expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });
  it('uses the separate operator key for scenario changes',async()=>{
    render(<App/>);await connect();fireEvent.click(screen.getByRole('button',{name:'Grid scenario'}));
    const dialog=screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Operator key'),{target:{value:'operator-only-key'}});
    fireEvent.change(within(dialog).getByLabelText('Grid scenario'),{target:{value:'clean'}});
    fireEvent.click(within(dialog).getByRole('button',{name:'Apply scenario'}));
    await screen.findByText('Demo grid scenario updated.');
    expect(posts[0]).toEqual({path:'/api/demo/scenario',key:'Bearer operator-only-key',body:{scenario:'clean'}});
    expect(within(dialog).getByLabelText('Operator key')).toHaveValue('');
  });
});
