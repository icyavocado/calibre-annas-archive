import lxml.html


SAMPLE_HTML = '''
<html>
  <body>
    <table>
      <tr>
        <td>
          <a tabindex="-1" href="/md5/11cc6e0bc61151c76da8cc3231faf479">
            <span><img src="https://example.com/covers/11cc6e0.jpg"/></span>
          </a>
        </td>
        <td><a><span>Butter</span></a></td>
        <td><a><span>Asako Yuzuki & Polly Barton (translator)</span></a></td>
        <td></td><td></td><td></td><td></td><td></td><td></td>
        <td><a><span>rtf</span></a></td>
      </tr>
    </table>
  </body>
</html>
'''


def test_search_row_parsing():
    doc = lxml.html.fromstring(SAMPLE_HTML)
    rows = doc.xpath('//table/tr')
    assert len(rows) == 1

    book = rows[0]
    columns = book.findall('td')
    assert len(columns) >= 10

    cover = columns[0].xpath('./a[@tabindex="-1"]')
    assert cover
    cover = cover[0]
    detail_item = cover.get('href', '').split('/')[-1]
    assert detail_item == '11cc6e0bc61151c76da8cc3231faf479'

    cover_url = ''.join(cover.xpath('(./span/img/@src)[1]'))
    assert cover_url == 'https://example.com/covers/11cc6e0.jpg'

    title = ''.join(columns[1].xpath('./a/span/text()'))
    assert title == 'Butter'

    author = ''.join(columns[2].xpath('./a/span/text()'))
    assert 'Asako Yuzuki' in author

    formats = ''.join(columns[9].xpath('./a/span/text()')).upper()
    assert formats == 'RTF'
