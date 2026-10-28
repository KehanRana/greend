import { useEffect,useState } from 'react';
export class ApiError extends Error { constructor(public status:number,message:string){super(message);} }
export async function request<T>(path:string,key:string,init:RequestInit={}):Promise<T>{
  const response=await fetch(path,{...init,headers:{'content-type':'application/json',authorization:`Bearer ${key}`,...init.headers}});
  if(!response.ok){
    let message=`Request failed (${response.status})`;
    try { message=(await response.json()).error?.message || message; } catch { /* Non-JSON ingress failure. */ }
    throw new ApiError(response.status,message);
  }
  return response.json() as Promise<T>;
}
export type Resource<T>={data?:T;error?:string;updated?:string;loading:boolean};
export function usePoll<T>(path:string,key:string,refresh:number):Resource<T>{
  const [state,setState]=useState<Resource<T>&{key:string}>({key:'',loading:false});
  useEffect(()=>{
    if(!key){setState({key:'',loading:false});return;}
    let active=true;
    const controller=new AbortController();
    let timer:ReturnType<typeof setTimeout>;
    async function poll(){
      setState(old=>old.key===key ? {...old,loading:!old.data} : {key,loading:true});
      try{
        const data=await request<T>(path,key,{signal:controller.signal});
        if(active)setState({key,data,loading:false,updated:new Date().toISOString()});
      }catch(error){
        if(active)setState(old=>({...old,key,loading:false,error:error instanceof Error ? error.message : 'Refresh failed'}));
      }
      if(active)timer=setTimeout(poll,3000);
    }
    void poll();
    return ()=>{active=false;controller.abort();clearTimeout(timer);};
  },[path,key,refresh]);
  return state.key===key ? state : {loading:!!key};
}
