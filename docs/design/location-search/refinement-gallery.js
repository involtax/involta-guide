for(const group of document.querySelectorAll('.refinement-list')){
  const tones=group.dataset.kind==='tone';
  const items=tones?window.SEARCH_REFINEMENTS.tones:window.SEARCH_REFINEMENTS.fonts;
  for(const item of items){
    const query='button=02&geometry=reference&tone='+(tones?item.id:'02')+'&font='+(tones?20:item.px);
    const card=document.createElement('section');card.className='variant-card';
    const heading=document.createElement('div');heading.className='variant-heading';
    const title=document.createElement('h2');
    if(tones){
      const number=document.createElement('span');number.className='variant-number';number.textContent=item.id;
      const hex=document.createElement('code');hex.textContent=item.hex;
      title.append(number,document.createTextNode(item.name),hex);
    }else title.textContent=item.px+' px · '+item.name;
    const link=document.createElement('a');link.href='index.html?'+query;link.target='_blank';link.rel='noopener';link.textContent='Открыть →';
    heading.append(title,link);
    const frame=document.createElement('iframe');frame.src='index.html?'+query+'&comparison=1&state=idle';
    frame.title=tones?item.name:item.px+' px';frame.tabIndex=-1;frame.inert=true;
    card.append(heading,frame);group.append(card);
  }
}
