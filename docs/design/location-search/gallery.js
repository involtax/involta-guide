const grid=document.querySelector('#variants');
for(const variant of window.SEARCH_VARIANTS){
  const card=document.createElement('section');card.className='variant-card';card.id='variant-'+variant.id;
  const heading=document.createElement('div');heading.className='variant-heading';
  const title=document.createElement('h2');
  const number=document.createElement('span');number.className='variant-number';number.textContent=variant.id;
  title.append(number,document.createTextNode(variant.name));
  const link=document.createElement('a');link.href='index.html?variant='+variant.id;link.target='_blank';link.rel='noopener';link.textContent='На главной →';
  heading.append(title,link);
  const description=document.createElement('p');description.className='variant-description';description.textContent=variant.description;
  const frame=document.createElement('iframe');frame.src='index.html?variant='+variant.id+'&gallery=1';frame.title=variant.id+' · '+variant.name;
  card.append(heading,description,frame);grid.append(card);
}
