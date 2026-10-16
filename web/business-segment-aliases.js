/* Presentation rules only. Semantic aliases must state their company and dimension. */
window.businessSegmentAliasRules = {
  version:'20261016-v1',
  aliases:[
    {code:'*',dimensions:['3'],name:'海外',aliases:['国外','海外'],reason:'地区同义词'},
    {code:'*',dimensions:['3'],name:'欧洲地区',aliases:['欧洲','欧洲地区'],reason:'地区名称规范'},
    {code:'*',dimensions:['3'],name:'美洲地区',aliases:['美洲','美洲地区'],reason:'地区名称规范'},
    {code:'*',dimensions:['3'],name:'亚太地区',aliases:['亚太','亚太地区'],reason:'地区名称规范'},
    {code:'605499',dimensions:['2'],name:'能量饮料',
      aliases:['能量饮料','东鹏特饮','销售东鹏特饮','销售东鹏特饮收入'],reason:'用户确认品牌名称与业务大类跨期对应'},
    {code:'601919',dimensions:['1','2'],name:'集装箱航运业务',
      aliases:['集装箱航运','集装箱航运业务','集装箱航运分部','集装箱航运及相关业务'],reason:'用户指出同一航运业务跨期更名，已核对本地交替披露'},
    {code:'601919',dimensions:['1','2'],name:'码头业务',
      aliases:['码头业务','码头业务分部','码头及相关业务','集装箱码头及相关业务'],reason:'同公司码头业务跨期展示别名'},
    {code:'601919',dimensions:['1','2','3'],name:'分部间抵销',
      aliases:['抵销','分部间抵销','分部间抵消','内部抵消','公司内各业务部间相互抵销'],reason:'同公司内部抵销名称规范'},
    {code:'601919',dimensions:['3'],name:'中国大陆',aliases:['中国地区','中国大陆'],reason:'同公司地区名称对齐，不推广为其他公司的境内外边界定义'}
  ],
  // Only these explicitly identified, disjoint business partitions may be added.
  regionalPartitions:[{
    code:'601919',dimension:'3',
    regions:{'中国地区':'中国大陆','中国大陆':'中国大陆','欧洲地区':'欧洲地区','美洲地区':'美洲地区','亚太地区':'亚太地区','其他国际地区':'其他国际地区'},
    businesses:['集装箱航运及相关业务','码头及相关业务'],
    reason:'同地区按航运、码头分列，按明确业务分项汇总'
  }]
};
