const grid=document.querySelector('#variants');
const buttons=document.body.dataset.gallery==='buttons';
for(const variant of (buttons?window.SEARCH_BUTTON_VARIANTS:window.SEARCH_VARIANTS)){
  const query=buttons?'variant=04&button='+variant.id:'variant='+variant.id;
  const card=document.createElement('section');card.className='variant-card';card.id='variant-'+variant.id;
  const heading=document.createElement('div');heading.className='variant-heading';
  const title=document.createElement('h2');
  const number=document.createElement('span');number.className='variant-number';number.textContent=variant.id;
  title.append(number,document.createTextNode(variant.name));
  const link=document.createElement('a');link.href='index.html?'+query;link.target='_blank';link.rel='noopener';link.textContent='На главной →';
  heading.append(title,link);
  const description=document.createElement('p');description.className='variant-description';description.textContent=variant.description;
  const frame=document.createElement('iframe');frame.src='index.html?'+query+'&gallery=1';frame.title=variant.id+' · '+variant.name;
  card.append(heading,description,frame);grid.append(card);
}
