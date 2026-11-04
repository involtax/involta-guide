// The server supplies public rows in baseline order. Only names enter this index.
// Matching/ranking is mirrored by domain/location_search.py for the native GET form.
window.GuideLocationIndex = catalogue => {
  const normalize = value => (Array.from(value).slice(0, 100).join('').normalize('NFKC')
    .toLowerCase().replaceAll('ё', 'е').match(/[\p{L}\p{N}]+/gu) || []).join(' ');
  const grams = value => {
    const result = new Set();
    for (const word of value.split(' ')) {
      const padded = Array.from('  ' + word + ' ');
      for (let i = 0; i < padded.length - 2; i++) result.add(padded.slice(i, i + 3).join(''));
    }
    return result;
  };
  const latin = 'qwertyuiopasdfghjklzxcvbnm', russian = 'йцукенгшщзфывапролдячсмить';
  const keyboard = value => /^[a-z ]+$/.test(value)
    ? normalize(Array.from(value, char => char === ' ' ? char : russian[latin.indexOf(char)]).join(''))
    : value;
  const index = catalogue.map((item, order) => {
    const name = normalize(item.name);
    return {item, order, name, words: name.split(' '), grams: grams(name)};
  });
  const direct = query => {
    const words = query.split(' '), short = query.replaceAll(' ', '').length < 3;
    return index.filter(row => short ? row.name.startsWith(query) : words.every(word => row.name.includes(word)))
      .map(row => ({...row, rank: row.name === query ? 0 : words.every(word => row.words.some(part => part.startsWith(word))) ? 1 : 2}))
      .sort((a, b) => a.rank - b.rank || a.order - b.order).map(row => row.item);
  };
  return value => {
    const query = normalize(value);
    if (!query) return [];
    let matches = direct(query);
    if (matches.length) return matches;
    const alternate = keyboard(query);
    if (alternate !== query) {
      matches = direct(alternate);
      if (matches.length) return matches;
    }
    if (alternate.length < 4) return [];
    const wanted = grams(alternate);
    return index.map(row => {
      let common = 0;
      for (const gram of wanted) if (row.grams.has(gram)) common++;
      return {...row, score: common / (wanted.size + row.grams.size - common)};
    }).filter(row => row.score >= .45)
      .sort((a, b) => b.score - a.score || a.order - b.order).map(row => row.item);
  };
};
