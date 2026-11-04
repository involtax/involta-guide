// Interactive visual prototype only: test data, no Guide endpoint or production navigation.
(() => {
  const params=new URLSearchParams(location.search);
  const buttonVariant=window.SEARCH_BUTTON_VARIANTS.find(item=>item.id===params.get('button'));
  const porcelain=window.SEARCH_VARIANTS.find(item=>item.id==='04');
  const variant=buttonVariant?porcelain:(window.SEARCH_VARIANTS.find(item=>item.id===params.get('variant'))||porcelain);
  const gallery=params.get('gallery')==='1';
  document.documentElement.dataset.searchVariant=variant.id;
  if(buttonVariant)document.documentElement.dataset.buttonVariant=buttonVariant.id;
  if(gallery)document.documentElement.dataset.preview='component';
  document.title=buttonVariant?'Кнопка '+buttonVariant.id+' · '+buttonVariant.name+' — Макет':variant.id+' · '+variant.name+' — Макет поиска';
  document.querySelector('.prototype-label').textContent=buttonVariant?'Макет · Фарфор · '+buttonVariant.id:'Макет '+variant.id+' · '+variant.name;
  if(buttonVariant&&!buttonVariant.arrow)document.querySelector('.search-submit > span:last-child').hidden=true;
  const records = [
    {name:'Великий Новгород',context:'Город · Новгородская область',alias:'Новгород',slug:'velikiy-novgorod'},
    {name:'Нижний Новгород',context:'Город · Нижегородская область',alias:'Нижний',slug:'nizhniy-novgorod'},
    {name:'Новосибирск',context:'Город · Новосибирская область',slug:'novosibirsk'},
    {name:'Новороссийск',context:'Город · Краснодарский край',slug:'novorossiysk'},
    {name:'Новомосковск',context:'Город · Тульская область',slug:'novomoskovsk'},
    {name:'Новый Уренгой',context:'Город · Ямало-Ненецкий автономный округ',slug:'novyy-urengoy'},
    {name:'Плёс',context:'Город · Ивановская область',slug:'plyos'},
    {name:'Переславль-Залесский',context:'Город · Ярославская область',slug:'pereslavl-zalesskiy'},
    {name:'Красная Поляна',context:'Направление · Краснодарский край',slug:'krasnaya-polyana'},
    {name:'Красная Поляна',context:'Курорт · Эсто-Садок, Краснодарский край',slug:'kurort-krasnaya-polyana'},
    {name:'Сочи',context:'Город · Краснодарский край',slug:'sochi'},
    {name:'Иваново',context:'Город · Ивановская область',slug:'ivanovo'},
  ];
  const form=document.querySelector('.destination-search');
  const input=document.querySelector('#destination');
  const status=document.querySelector('#search-status');
  const clear=document.querySelector('.search-clear');
  const footer=document.querySelector('.search-menu-footer');
  const panel=document.querySelector('.search-suggestions');
  const unavailable=params.get('state')==='error';
  const normalize=text=>text.toLowerCase().replaceAll('ё','е').trim();
  const safeHighlight=(element,text,query)=>{
    const at=normalize(text).indexOf(normalize(query));
    if(at<0){element.textContent=text;return;}
    element.append(document.createTextNode(text.slice(0,at)));
    const mark=document.createElement('mark');mark.textContent=text.slice(at,at+query.length);
    element.append(mark,document.createTextNode(text.slice(at+query.length)));
  };
  let widget;
  widget=new autoComplete({
    selector:'#destination',wrapper:false,threshold:2,debounce:180,submit:true,
    data:{src:async query=>unavailable?[]:records.filter(row=>normalize(row.name+' '+row.context+' '+(row.alias||'')).includes(normalize(query))),keys:['name'],cache:false},
    searchEngine:()=> ' ',
    resultsList:{class:'destination-results',destination:'.search-menu-footer',position:'beforebegin',maxResults:gallery?3:6,noResults:true,element:(list,data)=>{
      if(!data.results.length){
        const empty=document.createElement('li');empty.className='search-empty';empty.role='presentation';
        empty.textContent=unavailable?'Подсказки временно недоступны.':'Не нашли такую локацию. Попробуйте другое название.';list.append(empty);
      }
      status.textContent=data.results.length?`Подсказок: ${data.results.length}`:'Ничего не найдено';
      if(gallery&&data.results.length)requestAnimationFrame(()=>widget.goTo(0));
    }},
    resultItem:{class:'destination-result',element:(li,data)=>{
      li.replaceChildren();
      const text=document.createElement('span');
      const title=document.createElement('span');title.className='result-name';
      safeHighlight(title,data.value.name,input.value);
      const context=document.createElement('span');context.className='result-context';context.textContent=data.value.context;
      text.append(title,context);
      const arrow=document.createElement('span');arrow.className='result-arrow';arrow.textContent='→';arrow.ariaHidden='true';
      li.append(text,arrow);
    }},
    events:{input:{
      selection:event=>{
        const selected=event.detail.selection.value;
        input.value=selected.name;input.dataset.selected=selected.slug;widget.close();
        status.textContent='Демонстрация выбора: '+selected.name;clear.hidden=false;
      },
      keydown:event=>{
        if(event.isComposing)return;
        if(event.key==='Escape'){event.preventDefault();widget.close();return;}
        if(!widget.isOpen)return;
        if(event.key==='ArrowDown'){event.preventDefault();widget.next();}
        else if(event.key==='ArrowUp'){event.preventDefault();widget.previous();}
        else if(event.key==='Enter'&&widget.cursor>=0){event.preventDefault();widget.select(widget.cursor);}
      },
      open:()=>{panel.hidden=false;footer.hidden=false;},
      close:()=>{panel.hidden=true;footer.hidden=true;}
    }}
  });
  input.setAttribute('aria-autocomplete','list');
  input.addEventListener('input',()=>{clear.hidden=!input.value;delete input.dataset.selected;if(input.value.length<2){widget.close();status.textContent='';}});
  clear.addEventListener('click',()=>{input.value='';widget.close();clear.hidden=true;status.textContent='';input.focus();});
  form.addEventListener('submit',event=>{event.preventDefault();widget.close();status.textContent='Демонстрация поиска: '+input.value;input.dataset.submitted=input.value;});
  if(gallery){input.value='нов';clear.hidden=false;widget.start('нов');}
})();
